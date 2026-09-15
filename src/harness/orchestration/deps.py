"""装配层：把 `Suite` 翻译成 `RunSpec` + `RunDeps`，并发跑完所有用例。

## 这里是唯一知道所有具体实现的地方

`cli.py` 只跟本模块打交道；`Run` 只认识协议。suite 加载、并发调度、
provider 装配、评测器调度、store 选择，全部收在这一层 ——
所以 M6 把单 case 换成并发 suite，CLI 只多了一个 `--concurrency` 参数。

## 一次 suite 共用一个 store，不是一次 run 一个

`CompositeStore` 里的单写者队列是**跨 run 共享**的：8 路 run 各自 append，
由同一个后台任务批量落盘。一次 run 一个 store 的话，8 个写者线程又会
在 SQLite 上互相锁 —— 那正是单写者队列要解决的问题。

## 为什么 case 崩溃要显式抛错

调度器会把失败的 case 记成异常继续跑完其余的（这是对的：一条用例炸了
不该让另外四条的数据消失）。但**汇总时必须报出来** ——
静默丢掉一条 case，报告会缺一块，而缺的那块看起来和"这条用例没跑"一样。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from harness.contracts.protocols import EvalContext, LLMResponse
from harness.contracts.results import EvalResult
from harness.contracts.spec import RunRole, RunSpec, ToolPolicy
from harness.core.middleware.factory import build_middlewares
from harness.core.registry import ToolRegistry
from harness.core.run import Run, RunDeps, RunResult, default_run_id
from harness.core.tools.finish import FinishTool
from harness.core.tools.fs import ListDirTool, ReadFileTool, WriteFileTool
from harness.core.tools.search import SearchTool
from harness.core.tools.shell import RunCommandTool
from harness.evaluators.base import run_evaluators
from harness.orchestration.evalrunner import build_evaluators
from harness.orchestration.scheduler import Scheduler
from harness.orchestration.suite import CaseSpec, Suite, SuiteConfigError, load_suite
from harness.providers.fake import FakeProvider, text_response, tool_call_response
from harness.providers.recording import RecordingProvider, ReplayProvider
from harness.providers.response_pool import ResponsePool
from harness.store.composite import CompositeStore


class CaseExecutionError(RuntimeError):
    """有 case 以异常收场（不是 agent 失败，是执行本身炸了）。映射到退出码 1。"""


def build_tool_registry() -> ToolRegistry:
    """SUT 的完整工具集（设计文档 §3.3）。

    六个工具缺一不可：少了 `search`，agent 只能靠 `list_dir` 逐个目录翻；
    少了 `run_command`，它无法验证自己的修改。
    工具集的完整性直接决定**在测的是模型能力还是环境限制**。
    """
    reg = ToolRegistry()
    for tool in (ReadFileTool(), WriteFileTool(), ListDirTool(),
                 SearchTool(), RunCommandTool(), FinishTool()):
        reg.register(tool)
    return reg


def build_fake_script(raw: Any) -> list[LLMResponse]:
    """把 `fake_script` 配置转成 FakeProvider 的响应序列。

    元素形态：
        {tool: <name>, arguments: {...}}   触发工具调用
        {text: <str>}                       纯文本回复（会让 loop 终止为 no_finish）
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SuiteConfigError("fake_script must be a list")

    out: list[LLMResponse] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SuiteConfigError(f"fake_script[{i}] must be a mapping")
        if "tool" in item:
            args = item.get("arguments") or {}
            if not isinstance(args, dict):
                raise SuiteConfigError(f"fake_script[{i}].arguments must be a mapping")
            out.append(tool_call_response(str(item["tool"]), args, call_id=f"call_{i}"))
        elif "text" in item:
            out.append(text_response(str(item["text"])))
        else:
            raise SuiteConfigError(
                f"fake_script[{i}] needs either a 'tool' or a 'text' key"
            )
    return out


@dataclass(slots=True)
class RunOutcome:
    """一条 run 及其评测结果。

    合并成一个对象返回，而不是让调用方自己配对 —— 配对错了（比如结果错位）
    会让报告张冠李戴，且不会有任何报错。
    """

    result: RunResult
    evals: list[EvalResult] = field(default_factory=list)
    case_id: str = ""
    # 第几次重复。>1 时才能谈 flaky —— 只有把同一条用例的多次 run
    # 分开标注，`flaky_rate` 才有意义。
    repeat_index: int = 0


class RunBuilder:
    """装配层：加载 suite → 并发跑 case → 跑评测器 → 返回配对好的结果。"""

    def __init__(
        self,
        *,
        out_dir: Path | str = Path("runs"),
        record: Path | str | None = None,
        replay: Path | str | None = None,
        workdir: Path | str = "workdir",
    ) -> None:
        self.out_dir = Path(out_dir)
        self.record = Path(record) if record else None
        self.replay = Path(replay) if replay else None
        self.workdir = Path(workdir)

    # ---- provider ----
    def _build_provider(self, script: list[dict[str, Any]]) -> Any:
        """按 record/replay 模式包装 provider。

        装饰器模式让录制与厂商**解耦** —— provider 实现不知道录制功能存在。
        """
        if self.replay is not None:
            # 提前校验而非留给运行期：缺失的 cassette 是**配置错误**，
            # 不是 run 失败。若留给 ReplayProvider 抛 KeyError，loop 的异常兜底
            # 会把它转成 LLM_ERROR，最终 CLI 退出码为 0 —— 配置问题被伪装成正常结束。
            if not self.replay.exists():
                raise FileNotFoundError(f"replay cassette not found: {self.replay}")
            # 回放命不中时默认抛错而非静默降级（静默降级会让结果无声地错掉）
            return ReplayProvider(ResponsePool(self.replay))

        base = FakeProvider(build_fake_script(script))
        if self.record is not None:
            return RecordingProvider(base, ResponsePool(self.record))
        return base

    # ---- 入口 ----
    def run_suite_sync(
        self,
        suite_path: Path | str,
        *,
        evaluate: bool = False,
        concurrency: int | None = None,
        case_ids: list[str] | None = None,
    ) -> list[RunOutcome]:
        return asyncio.run(self.run_suite(
            suite_path, evaluate=evaluate, concurrency=concurrency, case_ids=case_ids
        ))

    async def run_suite(
        self,
        suite_path: Path | str,
        *,
        evaluate: bool = False,
        concurrency: int | None = None,
        case_ids: list[str] | None = None,
    ) -> list[RunOutcome]:
        suite = load_suite(suite_path)
        cases = _select_cases(suite, case_ids)

        # 缺失的 cassette 是**配置错误**，必须在任何 run 启动前抛错。
        # 放到 `_build_provider` 里迟了一步：那时它在调度器内部，会被当成
        # 某条 case 的执行失败（退出码 1），配置问题又一次伪装成了别的东西。
        if self.replay is not None and not self.replay.exists():
            raise FileNotFoundError(f"replay cassette not found: {self.replay}")

        tools = build_tool_registry()
        store = CompositeStore(root=self.out_dir)

        try:
            items: list[tuple[str, Any]] = []
            for case in cases:
                for rep in range(case.repeat):
                    items.append((
                        _key(case, rep),
                        partial(self._run_case, case=case, suite=suite, store=store,
                                tools=tools, evaluate=evaluate, repeat_index=rep),
                    ))

            scheduler = Scheduler(
                concurrency=(
                    concurrency if concurrency is not None
                    else suite.defaults.concurrency
                )
            )
            results = await scheduler.gather(items)
        finally:
            await store.close()

        # 崩溃的 case 必须显式报出来，不能静默丢
        failures = [(k, v) for k, v in results if isinstance(v, BaseException)]
        if failures:
            detail = "; ".join(f"{k}: {type(v).__name__}: {v}" for k, v in failures)
            raise CaseExecutionError(
                f"{len(failures)}/{len(results)} case(s) failed to execute: {detail}"
            )

        return [outcome for _, outcome in results]

    # ---- 单条 case ----
    async def _run_case(
        self,
        *,
        case: CaseSpec,
        suite: Suite,
        store: CompositeStore,
        tools: ToolRegistry,
        evaluate: bool,
        repeat_index: int,
    ) -> RunOutcome:
        spec = self._build_spec(case, suite)
        provider = self._build_provider(suite.fake_script_for(case))
        deps = RunDeps(
            provider=provider,
            store=store,
            tools=tools,
            middlewares=build_middlewares(suite.middlewares_for(case)),
            workdir=self.workdir,
            id_gen=default_run_id,
        )

        evals: list[EvalResult] = []
        try:
            result = await Run(spec, deps).execute()
            if evaluate:
                # 评测器在轨迹落盘之后跑 —— 它只读轨迹，与 agent 零耦合。
                # 只读是「评测器绝不 import core」这条架构约束的运行时体现：
                # 若哪天评测器想直接驱动 agent，这里就拿不到任何句柄。
                graders = build_evaluators(
                    [{"name": g.name, "config": g.config} for g in case.graders]
                )
                evals = await run_evaluators(graders, result.trajectory, EvalContext())
                # 落索引 —— M8 的聚合报告从这里读回，不用重跑评测
                await store.put_evals(result.run_id, evals)
        finally:
            # 录制内容必须在退出前落盘 —— 否则录了个寂寞
            if isinstance(provider, RecordingProvider):
                provider.save()

        return RunOutcome(result=result, evals=evals,
                          case_id=case.case_id, repeat_index=repeat_index)

    def _build_spec(self, case: CaseSpec, suite: Suite) -> RunSpec:
        return RunSpec(
            role=RunRole.SUT,
            system_prompt=suite.system_prompt_for(case),
            model=suite.defaults.model,
            task=case.task,
            tools=ToolPolicy(),
            # 名字进 spec（可序列化），实例进 RunDeps —— 这正是 MiddlewareSpec 的设计意图
            middlewares=suite.middlewares_for(case),
            budget=suite.budget_for(case),
            workspace=case.workspace,
            agent_name="sut",
            metadata={"case_id": case.case_id, "tier": case.tier, "tags": case.tags},
        )


def _key(case: CaseSpec, repeat_index: int) -> str:
    """调度器的 key。repeat=1 时就是 case_id，否则带下标。"""
    return case.case_id if case.repeat == 1 else f"{case.case_id}#{repeat_index}"


def _select_cases(suite: Suite, case_ids: list[str] | None) -> list[CaseSpec]:
    if not case_ids:
        return list(suite.cases)
    wanted = set(case_ids)
    known = {c.case_id for c in suite.cases}
    unknown = sorted(wanted - known)
    if unknown:
        # 点了不存在的 case 却静默跑完全部，是最坏的行为：
        # 你以为跑了 1 条，实际跑了 17 条，还花了 17 条的钱
        raise SuiteConfigError(
            f"unknown case id(s): {unknown}; known: {sorted(known)}"
        )
    return [c for c in suite.cases if c.case_id in wanted]


def dumps(obj: Any) -> str:
    """统一的 JSON 输出格式 —— 保证中文不被转义、key 有序。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
