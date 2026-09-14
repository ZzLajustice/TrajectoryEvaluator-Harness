# 任务 1：项目脚手架

> **所属里程碑**：M0 · **前置任务**：无 · **代码位置**：`pyproject.toml`、`.gitignore`、`.pre-commit-config.yaml`、`src/harness/__init__.py`、`src/harness/*/__init__.py`、`tests/__init__.py`

## 1. 总体目标

这个任务不是"建目录"，而是**在写第一行业务代码之前，把三条门禁立起来**：环境隔离（项目内 `.venv` + `uv.lock`）、架构约束（import-linter 的 2 条契约）、依赖栈锁定（ruff `banned-api` 禁止 `import httpx`）。

为什么必须先做：

- **HTTP 栈已经分叉**（tech-stack §0）：`httpx` 0.28.1 已停更，`openai` 3.13 与 `anthropic` 1.5 都依赖 pydantic 团队 fork 的 `httpx2` 2.12.0；而 `respx` / `litellm` / `instructor` 仍锁在旧栈，**同一虚拟环境无法共存**。如果不在第一天用 lint 禁掉旧栈，半年内一定有人在某个模块 `import httpx`，造成类型错误且极难定位。
- **架构约束必须可执行**：整份设计的支点是「`evaluators` 绝不 import `core`」（设计文档 §2.4）。这条规则写在文档里没有任何强制力，必须变成 `lint-imports` 能报 `0 broken` 的契约。
- **结果可复现**：`uv.lock` + PEP 735 `[dependency-groups]` 完全替代 requirements.txt，`uv sync` 后任何人拿到同一份依赖。

不解决会怎样：依赖漂移、评测数字不可比、架构在三个月后退化成"什么都能 import 什么"。

## 2. 实现流程

1. `uv python install 3.12` → `uv venv --python 3.12` → `git init`
2. 写 `pyproject.toml`（内容直接抄 tech-stack §11）
3. 写 `.gitignore`
4. `uv sync --all-groups` + import 冒烟验证
5. `uv run lint-imports` 拿基线
6. commit

为什么是这个顺序：

- **venv 先于 sync**：不建环境，`uv sync` 没有落点。
- **`.gitignore` 必须早于任何 `git add`**：`.venv/` 有几千个文件，一旦进入索引，清理成本远高于先写 ignore。同理 `runs/`、`workdir/`，以及绝不能进版本库的 `.env`（凭据）。
- **第 5 步是整条流程里最重要的一步**：此时仓库里还没有任何 harness 代码，`lint-imports` 必然报 `Contracts: 2 kept, 0 broken.`。它的目的**不是检查，而是固定基线**——只有当"绿"是已知基线时，之后的变红才精确等于"这次改动破坏了架构"。若等到有代码后再首跑，你无法区分"我这行写错了"和"契约一开始就配错了"。
- **`--all-groups` 而非默认**：`ruff` / `pyright` / `import-linter` 都在 dev 组，不装 dev 组第 4、5 步跑不起来。

## 3. 具体技术实现

**`dynamic = ["version"]` + `hatch-vcs`**：版本从 git tag 推导，不手写。代价是必须用标准 `src/harness` 布局，且构建后端不能选 `uv_build`——它零配置，但**不支持动态元数据**（`dynamic = ["version"]` 直接失败），也不支持把 HTML 模板与内联图表 JS 作为 package data 打进 wheel（tech-stack §10.2）。

**`[project.scripts] harness = "harness.cli:app"`** 可以在 `harness/cli.py` 还不存在时先声明——entry point 只在安装时解析，不影响本任务的 `uv sync`。

**`[tool.uv] exclude-newer = "7 days"`**：只解析发布满 7 天的版本（照抄 langfuse 的供应链防护）。恶意包最常见的攻击窗口就是发布后几小时。

**版本区间要写成"带排除的区间"**，因为约束本身就是踩坑历史的编码（tech-stack §4.1、§6）：

| 约束 | 排除的是什么 |
|---|---|
| `anyio>=4.14,!=4.15.0,<5` | 4.14 修了 asyncio Lock/Semaphore 取消后 deadlock；4.15.0 被 mlflow 排除 |
| `rich>=14.1,<16` | 避开 14.0.0 与生态尚未跟上的 15.x |
| `click>=8.1.3,!=8.2.0,!=8.2.2,!=8.3.0,!=8.3.1` | 8.2.2 / 8.3.0 / 8.3.1 破坏 optional flag values（typer 会传递性踩到） |

**`banned-api` 禁 `httpx`** 是脚手架里唯一"防未来"的配置，写在 `[tool.ruff.lint.flake8-tidy-imports.banned-api]`，报错文本直接给出正确做法（`Use httpx2`）。反例：只写在 README 里 → 没人读；正解：让 lint 报错，并把原因写进报错文本。

## 4. 使用的技术栈简介

| 工具 | 版本 | 作用 | 为什么是它 |
|---|---|---|---|
| `uv` | 0.12.13 | 装 Python、建 venv、装依赖、锁版本 | `pip-tools` 已于 2026-03 被 Jazzband 宣布 sunset；Open edX 的 OEP-67 直接把 uv + pyproject 定为后端标准 |
| `hatchling` + `hatch-vcs` | — | 构建后端 + 从 git tag 取版本 | `uv_build` 不支持动态元数据与 package data |
| `ruff` | 0.16.x | lint / format | 事实标准：inspect_ai、lm-eval、ragas、mlflow、langfuse 全在用 |
| `pyright` | 1.1.414 | 类型检查（CI gate） | 本项目 pydantic 密集，`ty` 0.0.x 无 plugin 会误报 |
| `import-linter` | 2.15 | 架构约束（`forbidden` / `layers`） | 用 Grimp（Rust）建静态导入图，一条 `forbidden` 就能表达"评测器不依赖 core" |
| `pre-commit` | 4.6.x | 提交前钩子 | 标准 |

## 5. 工程化思想

**（1）门禁要在第一次变红之前建立。** 在基线为绿的时刻固定契约，之后每次红灯都精确指向一次改动。可迁移到任何项目：CI 的第一步不是"跑测试"，而是"确认工具链在当前状态是绿的"。

**（2）声明式约束 + 可执行断言，冗余是刻意的。** `import-linter`（配置即文档，给 CI 用）与 `tests/test_architecture.py`（纯 ast，能在断言消息里给出违规文件路径）检查同一件事，但失败时可读性不同：一个给你契约名，一个给你行号。当两种工具成本都低、失败模式互补时，不要为了"消除重复"砍掉一个。

**（3）依赖版本区间是踩坑历史的编码。** `anyio!=4.15.0` 看起来丑，但它把一个小时的 debug 结论固化成了机器可执行的规则。反模式是只写 `anyio>=4.14`，然后下一个人踩到同一个坑再查一遍。

**（4）环境隔离的第一性理由是"结果可复现"。** harness 的产出是数字（pass_rate / 成本 / 步数），依赖漂移会让历史报告失去可比性。这条原则对任何"产出需要被比较"的项目都成立。
