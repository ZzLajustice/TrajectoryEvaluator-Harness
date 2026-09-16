"""架构约束的可执行契约。

## 与 `lint-imports` 是**刻意冗余**的

两者都验证同一张依赖表，但服务不同场景：

| | `lint-imports` | 本文件 |
|---|---|---|
| 给谁用 | CI（配置即文档，`pyproject.toml` 里一眼可见） | 单测（失败时能精确指出**违规文件 + import 语句**） |
| 依赖 | import-linter | 纯 `ast`，零运行时依赖 |

冗余在这里是**特性**：架构约束是本项目最容易被无意破坏的东西
（一次"就从 evaluators import 一下 core 省事"就够了），
两条独立防线比一条难以察觉的防线可靠。

## 为什么用 `ast` 而不是 import 后检查模块对象

import 会**执行**被测模块（副作用、循环依赖、甚至需要密钥）。
`ast` 只看源码文本，不执行任何东西，也不受运行时状态影响。

## 本文件不检查什么

只检查 `harness.*` 内部的依赖边。第三方包不在约束范围内 ——
`pyproject.toml` 的 dependencies 列表管那个。
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "harness"

# 评测器**绝不允许**碰的包。这是本项目架构设计的支点：
# 评测器只认 `contracts/` 里的协议，真实实现由组装层注入。
FORBIDDEN_FOR_EVALUATORS = {
    "harness.core", "harness.orchestration", "harness.providers",
    "harness.store", "harness.report", "harness.adapters", "harness.cli",
}

# 每层允许 import 的 harness 包（含自身）。
#
# ⚠️ `events` 是最底层：**只能 import 自己**。设计文档与 import-linter 的
# layers 契约都把 `contracts` 排在 `events` 之上（只允许 contracts → events）。
# 曾误放过 events → contracts，那会允许 events ↔ contracts 成环。
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "events": {"harness.events"},
    "contracts": {"harness.events", "harness.contracts"},
    "evaluators": {"harness.events", "harness.contracts", "harness.evaluators"},
    "core": {"harness.events", "harness.contracts", "harness.core"},
    "store": {"harness.events", "harness.contracts", "harness.store"},
    "providers": {"harness.events", "harness.contracts", "harness.providers"},
    "adapters": {"harness.events", "harness.contracts", "harness.adapters"},
    "report": {"harness.events", "harness.contracts", "harness.store",
               "harness.report"},
    # 测试工具是**对外发布的产品能力**（见 testing/builder.py 的 docstring），
    # 因此它也必须待在 L0 之上：只能用 events 与 contracts。
    "testing": {"harness.events", "harness.contracts", "harness.testing"},
}

# 组装层。它们**就是**允许知道一切的那一层（依赖注入的落点），
# 所以不写进 ALLOWED_IMPORTS —— 写个"允许所有包"的集合等于没写。
ASSEMBLY_LAYERS = {"orchestration", "cli"}


def _imports(path: pathlib.Path) -> set[str]:
    """收集一个文件里所有**绝对** import 的模块名。

    相对 import（`level > 0`）不返回：包内相对导入不可能跨层越界，
    且解析它需要知道包结构 —— 徒增复杂度而没有收益。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def _violations(pkg: str, forbidden: set[str]) -> list[str]:
    root = SRC / pkg
    if not root.exists():
        return []
    found = []
    for py in sorted(root.rglob("*.py")):
        for mod in sorted(_imports(py)):
            if any(mod == f or mod.startswith(f + ".") for f in forbidden):
                found.append(f"{py.relative_to(SRC)}: import {mod}")
    return found


def _layer_violations(pkg: str) -> list[str]:
    """某层里所有越出白名单的 harness import（层或包不存在时返回空）。

    `harness.contracts.protocols` 对比白名单里的 `harness.contracts`：
    按前两段取顶层包名再比，否则子模块会被误判成违规
    （**这条测试最初就写错了** —— 把完整模块路径直接塞进 `in` 判断）。
    """
    allowed = ALLOWED_IMPORTS.get(pkg, set())
    root = SRC / pkg
    if not root.exists():
        return []
    found = []
    for py in sorted(root.rglob("*.py")):
        for mod in sorted(_imports(py)):
            if not mod.startswith("harness."):
                continue
            if ".".join(mod.split(".")[:2]) not in allowed and mod not in allowed:
                found.append(f"{py.relative_to(SRC)}: import {mod}")
    return found


# ---- 支点：评测器与 core 零耦合 ----
def test_evaluators_do_not_import_core():
    """★ 本项目架构设计的支点 —— 评测器与被测 agent 零耦合。

    违反它的典型动机是"我就想在评测器里直接跑一次 run 做 LLM 兜底"。
    正确做法是注入 `contracts.protocols.JudgeClient`。
    """
    violations = _violations("evaluators", FORBIDDEN_FOR_EVALUATORS)
    assert not violations, (
        "evaluators 不得依赖 core/orchestration/store/providers/adapters/cli:\n  "
        + "\n  ".join(violations)
        + "\n\n需要跑 judge 请用 harness.contracts.protocols.JudgeClient。")


def test_evaluators_only_depend_on_l0():
    """比上一条更严：评测器只能向下 import events / contracts。

    与 `test_layer_imports_match_whitelist` 同源，但独立断言 ——
    这条挂了想让读的人第一眼看到「评测器越界了」，
    而不是淹没在全表的遍历里。
    """
    violations = _layer_violations("evaluators")
    assert not violations, (
        "evaluators 只允许 import events / contracts：\n  "
        + "\n  ".join(violations))


# ---- L0 叶子层 ----
def test_events_is_the_bottommost_layer():
    """Events 不得 import 任何其他 harness 包 —— **包括 contracts**。

    contracts 位于 events 之上，因此 events → contracts 会成环。
    """
    violations = _violations("events", {"harness.contracts", "harness.core",
                                        "harness.orchestration", "harness.store",
                                        "harness.providers", "harness.evaluators",
                                        "harness.report", "harness.adapters",
                                        "harness.testing", "harness.cli"})
    assert not violations, (
        "events 是最底层，只能 import 自己：\n  " + "\n  ".join(violations))


def test_contracts_are_a_leaf_layer():
    """Contracts 只允许向下依赖 events。"""
    violations = _violations("contracts", {"harness.core", "harness.orchestration",
                                           "harness.store", "harness.providers",
                                           "harness.evaluators", "harness.report",
                                           "harness.adapters", "harness.testing",
                                           "harness.cli"})
    assert not violations, (
        "contracts 只能依赖 events：\n  " + "\n  ".join(violations))


def test_only_orchestration_and_cli_may_import_everything():
    """组装层是**唯一**允许知道所有实现的层（依赖注入的落点）。

    这条测试换个角度表述同一件事：除 orchestration / cli 外，
    没有任何一层能 import orchestration。
    """
    for pkg in ("events", "contracts", "evaluators", "core", "store",
                "providers", "adapters", "report", "testing"):
        violations = _violations(pkg, {"harness.orchestration"})
        assert not violations, (
            f"{pkg} 不得 import 组装层（会形成环）：\n  " + "\n  ".join(violations))


# ---- 全表白名单 ----
def test_layer_imports_match_whitelist():
    for pkg in ALLOWED_IMPORTS:
        violations = _layer_violations(pkg)
        assert not violations, (
            f"层 {pkg!r} 越出依赖白名单：\n  " + "\n  ".join(violations)
            + f"\n允许：{sorted(ALLOWED_IMPORTS[pkg])}")


def test_the_whitelist_covers_every_layer_on_disk():
    """★ 反向检查：白名单不能漏掉真实存在的层。

    没有这条，"新加一个包但忘了加进白名单"会让它的依赖边**完全不被检查** ——
    而没被检查与通过，在测试输出里长得一模一样。这个洞比一条违规更危险。

    组装层除外：它们本来就允许 import 一切（见 `ASSEMBLY_LAYERS`），
    靠 `test_only_orchestration_and_cli_may_import_everything` 的反面来约束。
    """
    on_disk = {p.name for p in SRC.iterdir()
               if p.is_dir() and (p / "__init__.py").exists()}
    missing = on_disk - set(ALLOWED_IMPORTS) - ASSEMBLY_LAYERS
    assert not missing, (
        f"这些包在磁盘上存在，却不在 ALLOWED_IMPORTS 里：{sorted(missing)}。"
        " 加进去，否则它们的依赖边完全不受约束。")
