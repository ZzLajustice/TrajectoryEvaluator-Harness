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

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "harness"

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


# --------------------------------------------------------------------------
# 层编号：它只是助记，但**必须是被检查过的**助记
# --------------------------------------------------------------------------
#
# 设计文档里有一套 `L0/L1/L2/L3/L6` 的编号，而代码里从来没有编号 ——
# 于是那套编号既无处校验、也与实际分层对不上（比如 `L6` 夹在 `L3` 和 `L1` 之间）。
#
# 这里把它定死成一张**有序的层表**，并加一条断言让它可被检查。
# 断言的形式刻意是**子集**而不是相等：`LAYERS` 是比白名单更粗的助记
# （它只知道"层"，不知道 report 只认 store 而不认 core），
# 而"白名单从不指向更高层"是它唯一需要保证的性质。
LAYERS: tuple[tuple[str, ...], ...] = (
    ("events",),                                                  # 0
    ("contracts",),                                               # 1
    ("core", "store", "providers", "evaluators", "adapters", "testing"),   # 2
    ("report",),                                                  # 3
    ("orchestration",),                                           # 4
    ("cli",),                                                     # 5
)

#: 组装层不受白名单约束（它就是知道一切的那一层），因此不参与这项检查。
_UNCONSTRAINED = set(ASSEMBLY_LAYERS)


def _tier_of(pkg: str) -> int:
    for index, tier in enumerate(LAYERS):
        if pkg in tier:
            return index
    raise AssertionError(f"{pkg} 不在 LAYERS 里 —— 加了新包就要加进这张表")


def test_every_package_is_placed_in_the_layer_table():
    """★ 反向检查：磁盘上的每个包都得在 `LAYERS` 里有一个位置。

    没有这条，新加的包会**静默地不在编号体系里** —— 而它看起来和
    "这个包不参与编号"一模一样。`testing/` 就是这样从设计文档的编号里漏掉的。
    """
    on_disk = {p.name for p in SRC.iterdir()
               if p.is_dir() and (p / "__init__.py").exists()}
    missing = on_disk - {n for tier in LAYERS for n in tier}
    assert not missing, (
        f"这些包不在 LAYERS 里：{sorted(missing)}。"
        f" 加进去，否则它们不参与层编号的检查。")


def test_the_layer_table_agrees_with_the_whitelist():
    """★ 白名单**从不指向更高层**（同级只允许指向自己）。

    这条把编号从"文档里的说法"变成"被检查的声明"。写反了（或新加的边
    指向上层）就会红 —— 而症状本来会是"文档与代码不符，靠读者记住哪套算"。

    用**子集**而不是相等是有意的：`LAYERS` 只表达"层"，表达不了
    "report 只认 store 而不认 core"这种同层内的差异。白名单是权威，
    编号是助记 —— 助记只需要不与权威冲突。
    """
    for pkg, allowed in ALLOWED_IMPORTS.items():
        for mod in allowed:
            other = mod.removeprefix("harness.")
            assert _tier_of(other) <= _tier_of(pkg), (
                f"白名单里 {pkg} → {other} 指向了**更高**的层"
                f"（{_tier_of(other)} > {_tier_of(pkg)}）。"
                f" 要么改白名单，要么改 LAYERS —— 但两者不能不一致。")


# --------------------------------------------------------------------------
# import-linter 必须与白名单**等价**，而不是比它宽
# --------------------------------------------------------------------------
def _import_linter_contracts() -> dict[str, list[str]]:
    """从 `pyproject.toml` 解析出逐包的 forbidden 契约。

    只认名字形如 `<包> layer edges` 的那些 —— 命名约定让"这两份表是同一件事"
    在配置里一眼可见，也让这条测试知道该比对哪些。
    """
    import tomllib

    raw = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for contract in raw["tool"]["importlinter"]["contracts"]:
        name = contract.get("name", "")
        if not name.endswith(" layer edges"):
            continue
        pkg = name.removesuffix(" layer edges")
        assert contract["type"] == "forbidden", name
        out[pkg] = [m.removeprefix("harness.") for m in contract["forbidden_modules"]]
    return out


def test_import_linter_mirrors_the_whitelist():
    """★★ 两份表必须**等价** —— "刻意冗余"只有在等价时才有价值。

    这里曾经是一条 `layers` 契约，而它**比架构测试宽**。不是配错了，
    是类型选错了：`layers` 只表达得了全序，而真实依赖图是 **DAG**
    （`report → store` 成立，而 `core` 与 `report` 互不可见）——
    任何线性排列都会放行一条白名单禁止的边。

    **实测**（2026-09-18）：往 `evaluators/base.py` 加一行
    `from harness.core import workspace`，即架构的支点被破坏：

        layers 契约（旧）    → KEPT，整个 lint-imports 门是绿的
        forbidden 契约（新） → BROKEN，两条契约同时抓到

    也就是说旧配置下，`uv run lint-imports` 会在架构被破坏时**报绿** ——
    而读那份配置的人会以为它守住了。

    这条测试让等价性成为**被检查的**性质：改了白名单却忘了改 pyproject
    （或反过来）会立刻红。
    """
    contracts = _import_linter_contracts()
    all_pkgs = set(ALLOWED_IMPORTS) | _UNCONSTRAINED

    assert set(contracts) == set(ALLOWED_IMPORTS), (
        f"import-linter 的契约与白名单不是同一组包："
        f"\n  只在契约里: {sorted(set(contracts) - set(ALLOWED_IMPORTS))}"
        f"\n  只在白名单里: {sorted(set(ALLOWED_IMPORTS) - set(contracts))}")

    for pkg, forbidden in contracts.items():
        expected = {q for q in all_pkgs
                    if q != pkg
                    and f"harness.{q}" not in ALLOWED_IMPORTS[pkg]}
        assert set(forbidden) == expected, (
            f"{pkg} layer edges 与白名单不等价："
            f"\n  契约多禁了: {sorted(set(forbidden) - expected)}"
            f"\n  契约漏禁了: {sorted(expected - set(forbidden))}"
            f"\n  （漏禁 = 破坏这条边时 lint-imports 会报绿）")


# --------------------------------------------------------------------------
# 两条反直觉的边界：它们必须是**被检查的**，不能只靠人记住
# --------------------------------------------------------------------------
def test_core_cannot_see_store_despite_both_being_mid_tier():
    """★ `core` 与 `store` 在 `LAYERS` 里是**同一层**，却互不可见。

    这条反直觉，而它有两个具体后果，都值得单独钉住：

      - `core` 要读轨迹只能拿注入的 `TrajectoryStore` **协议**，
        不能 import 具体实现 —— 否则 `Run` 就没法在测试里换成假 store
      - 反方向同样成立：`store` 不许 import `core`，
        所以「工作目录路径」这类常量只能住在 `core`（`Workspace` 与
        组装层都要用，而 core 看不见 store）。**这一条我踩过** ——
        把常量放进 `store/layout.py` 时 `lint-imports` 直接红。
    """
    for pkg, other in (("core", "store"), ("store", "core")):
        violations = _violations(pkg, {f"harness.{other}"})
        assert not violations, (
            f"{pkg} 不得 import {other} —— 它们是同一层但不是一条链：\n  "
            + "\n  ".join(violations))


def test_report_cannot_see_orchestration():
    """★ `report` 看不见 `orchestration`，尽管它在依赖图上「更高」。

    这正是 `latest.json` / `index.db` 这类文件名住在 `store/layout.py`
    而不是 `orchestration/` 的原因：命名常量必须待在一个**报告层能看见**
    的位置。放错地方的症状不是报错，而是有人为了拿到常量去 import 组装层。
    """
    violations = _violations("report", {"harness.orchestration"})
    assert not violations, (
        "report 不得 import 组装层：\n  " + "\n  ".join(violations))
    # 交叉验证：这条边界同时写在**权威表**里，不是只活在助记里。
    # 依赖是单向的 —— 组装层调报告，报告看不见组装层。
    assert "harness.orchestration" not in ALLOWED_IMPORTS["report"]
    assert _tier_of("report") < _tier_of("orchestration"), (
        "报告层必须排在组装层**之下**：组装层调报告，反过来不行")
