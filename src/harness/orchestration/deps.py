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

from harness.contracts.protocols import EvalContext, JudgeCase, LLMResponse
from harness.contracts.results import EvalResult
from harness.contracts.spec import ModelRef, RunRole, RunSpec, ToolPolicy
from harness.core.middleware.factory import build_middlewares
from harness.core.registry import ToolRegistry
from harness.core.run import Run, RunDeps, RunResult, default_run_id
from harness.core.tools.finish import FinishTool
from harness.core.tools.fs import ListDirTool, ReadFileTool, WriteFileTool
from harness.core.tools.search import SearchTool
from harness.core.tools.shell import RunCommandTool
from harness.evaluators.base import run_evaluators
from harness.events.trajectory import Trajectory
from harness.orchestration.aggregator import (
    aggregate,
    to_case_outcomes,
    write_snapshot,
)
from harness.orchestration.credentials import is_fake, resolve_endpoint
from harness.orchestration.evalrunner import build_evaluators
from harness.orchestration.judge import (
    JudgeConfig,
    RunBasedJudgeClient,
    build_judge_tools,
    meta_trajectory,
)
from harness.orchestration.scheduler import Scheduler, Skipped
from harness.orchestration.suite import CaseSpec, Suite, SuiteConfigError, load_suite
from harness.providers.fake import FakeProvider, text_response, tool_call_response
from harness.providers.openai_compat import OpenAICompatProvider
from harness.providers.recording import RecordingProvider, ReplayProvider
from harness.providers.response_pool import ResponsePool
from harness.store.composite import CompositeStore
from harness.store.layout import SNAPSHOT_NAME


class CaseExecutionError(RuntimeError):
    """有 case 以异常收场（不是 agent 失败，是执行本身炸了）。映射到退出码 1。"""


class SuiteCostExceeded(RuntimeError):
    """suite 级成本上限被击穿，剩余 case 未启动。映射到退出码 3。

    **不是 per-case 的失败**：`Budget.max_usd` 已经管住单条 case 跑飞；
    这条管的是"17 条用例一共花了多少"。两者的报警对象不同。
    """


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
        # suite 级累计成本。只有 "suite 一共花了多少" 这类门禁需要它，
        # 单条 case 的成本在 RunResult.usage 里。
        self._spent_usd = 0.0

    # ---- 装配期校验 ----
    def _preflight(self, suite: Suite) -> None:
        """在任何 run 启动前，把"跑起来必然会炸"的配置问题全部抛出来。

        每一条都在 `_run_case` 里有对应实现 —— 这里只是**提前**跑一遍。
        重复解析一次凭据的代价可以忽略，换来的是"配置错误"永远是退出码 2。
        """
        if self.replay is not None and not self.replay.exists():
            raise FileNotFoundError(f"replay cassette not found: {self.replay}")

        if self.replay is None:
            # 真 provider 的凭据：缺 key / 厂商未知都在这里暴露
            if not is_fake(suite.defaults.model.provider):
                resolve_endpoint(suite.defaults.model, role="sut")
            judge = suite.judge_config()
            if judge is not None and not is_fake(judge.provider):
                resolve_endpoint(
                    ModelRef(provider=judge.provider, model=judge.model), role="judge"
                )

    # ---- provider ----
    def _build_provider(self, model: ModelRef, script: list[dict[str, Any]],
                        *, role: str = "sut") -> Any:
        """按 `ModelRef.provider` 分派，再按 record/replay 模式包装。

        装饰器模式让录制与厂商**解耦** —— provider 实现不知道录制功能存在。

        ## 真 provider 的凭据在这里解析，不在 suite 里

        `ModelRef` 刻意**没有** api_key 字段：suite 文件是入库的。
        凭据只能来自环境变量或 `.env`（见 `credentials.py` 的阶梯），
        缺失时抛 `ProviderConfigError` → CLI 退出码 2。

        在**装配阶段**就解析而不是等第一次调用：这样 key 没配是
        "配置错误"，而不是跑了一半的 `llm_error`。
        """
        if self.replay is not None:
            # 提前校验而非留给运行期：缺失的 cassette 是**配置错误**，
            # 不是 run 失败。若留给 ReplayProvider 抛 KeyError，loop 的异常兜底
            # 会把它转成 LLM_ERROR，最终 CLI 退出码为 0 —— 配置问题被伪装成正常结束。
            if not self.replay.exists():
                raise FileNotFoundError(f"replay cassette not found: {self.replay}")
            # 回放命不中时默认抛错而非静默降级（静默降级会让结果无声地错掉）
            return ReplayProvider(ResponsePool(self.replay))

        base: Any
        if is_fake(model.provider):
            base = FakeProvider(build_fake_script(script))
        else:
            endpoint = resolve_endpoint(model, role=role)
            base = OpenAICompatProvider(
                api_key=endpoint.api_key, base_url=endpoint.base_url
            )

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
        max_cost: float | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> list[RunOutcome]:
        return asyncio.run(self.run_suite(
            suite_path, evaluate=evaluate, concurrency=concurrency,
            case_ids=case_ids, max_cost=max_cost, model=model, provider=provider,
        ))

    async def run_suite(
        self,
        suite_path: Path | str,
        *,
        evaluate: bool = False,
        concurrency: int | None = None,
        case_ids: list[str] | None = None,
        max_cost: float | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> list[RunOutcome]:
        suite = load_suite(suite_path)
        # 命令行覆盖 suite 的 defaults —— 换模型不该逼人改 suite 文件
        if model or provider:
            suite.defaults.model = suite.defaults.model.model_copy(update={
                **({"model": model} if model else {}),
                **({"provider": provider} if provider else {}),
            })
        cases = _select_cases(suite, case_ids)

        # ★ 装配期能失败的东西，一律在**调度器之前**校验。
        #
        # 为什么这条反复出现：调度器的职责是**隔离运行期故障**
        # （网络抽风、沙箱炸了），它把异常记成"某条 case 失败"。
        # 但配置错误被它接住之后，症状就变成了"1/1 case failed to execute"
        # 配退出码 1（门禁未达标）—— 让该去改配置的人去查门禁。
        #
        # 已经栽过三次，每次都是同一个形状：
        #   1. replay cassette 缺失（M6）
        #   2. judge 的 case_id 含冒号（M9，还是在沙箱建立时炸的）
        #   3. 真模型的 API key 没配（现在）
        # 所以这里的规则是：**任何在 `_run_case` 里可能抛的装配错误，
        # 都要在这里先抛一遍。**
        self._preflight(suite)

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
            results = await scheduler.gather(
                items,
                # suite 级成本上限：超了就不再启动新 case。
                # 已经在跑的那几条让它们跑完 —— 中途掐断会留下半截沙箱，
                # 而"少跑一条"比"跑一条半"更容易解释。
                should_stop=(
                    (lambda: self._spent_usd >= max_cost)
                    if max_cost is not None else None
                ),
            )
        finally:
            await store.close()

        # 成本超限：剩下的没跑。必须显式报出来 —— 静默返回一部分结果
        # 会让"只跑了 5 条 / 共 17 条"看起来像"17 条都跑了"。
        skipped = [k for k, v in results if isinstance(v, Skipped)]
        if skipped:
            raise SuiteCostExceeded(
                f"suite cost {self._spent_usd:.4f} exceeded cap {max_cost}; "
                f"{len(skipped)} case(s) not started: {skipped}"
            )

        # 崩溃的 case 必须显式报出来，不能静默丢
        failures = [(k, v) for k, v in results if isinstance(v, BaseException)]
        if failures:
            detail = "; ".join(f"{k}: {type(v).__name__}: {v}" for k, v in failures)
            raise CaseExecutionError(
                f"{len(failures)}/{len(results)} case(s) failed to execute: {detail}"
            )

        outcomes = [outcome for _, outcome in results]
        self._warn_if_cost_cap_is_inert(outcomes, max_cost,
                                        suite.defaults.model.provider)
        # 落 case 级快照 —— diff / ci / report 都从它读。
        # 在 run_suite 里写而不是让 CLI 记得写：忘了写的话 diff 会安静地
        # 拿一份过期的基线去比，而那看起来像"没有回归"。
        self._write_snapshot(outcomes, suite)
        return outcomes

    def _warn_if_cost_cap_is_inert(
        self, outcomes: list[RunOutcome], max_cost: float | None, provider: str
    ) -> None:
        """真模型 + 设了成本上限 + 全部报 0 成本 → 明说这个上限没生效。

        ## 为什么必须吵一声

        厂商价格表没实现，所以 `Usage.cost_usd` 对真实模型恒为 0。
        后果是 `--max-cost` 与 `Budget.max_usd` **都是失效的** ——
        但它们看起来像在保护你，这是最坏的一种安全机制。

        ## 为什么必须区分 fake

        `FakeProvider` 也报 token（15/次）也报 0 成本 —— 但**没有花任何钱**，
        警告它纯属误报。而一个会误报的警告等于没有警告：
        看多了就学会了忽略。

        初版就是按"tokens > 0 且 cost == 0"判的，于是三条 fake 用例的测试
        全被它刷了一遍。判据必须是**真的有外部花费**，那就要看 provider 是不是真的。
        """
        if max_cost is None or is_fake(provider):
            return
        tokens = sum(o.result.usage.input_tokens + o.result.usage.output_tokens
                     for o in outcomes)
        spent = sum(o.result.usage.cost_usd for o in outcomes)
        if tokens > 0 and spent == 0.0:
            import warnings

            warnings.warn(
                f"cost cap {max_cost} could not be enforced: {tokens} tokens were "
                "spent but every run reported cost_usd=0. There is no vendor price "
                "table, so Usage.cost_usd is always 0 for real models and both "
                "--max-cost and Budget.max_usd are inert. See docs/known-gaps.md.",
                RuntimeWarning,
                stacklevel=2,
            )

    def _write_snapshot(self, outcomes: list[RunOutcome], suite: Suite) -> None:
        cases = to_case_outcomes(outcomes)
        write_snapshot(aggregate(cases), cases, self.out_dir / SNAPSHOT_NAME,
                       suite_name=suite.name)


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
        provider = self._build_provider(spec.model, suite.fake_script_for(case))
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
                #
                # ★ `MetaEvaluator` 必须**排除在常规这一轮之外** ——
                # 它的评测对象是 judge，不是 SUT。留在里面的话它会跑两次：
                # 一次拿 SUT 轨迹（找不到任何判定 → 0 verdict 却报 PASS，
                # 一个看起来正常但毫无意义的结果），一次拿 judge 轨迹。
                # 实测踩过：`evals[0]` 恰好是那个没意义的，指标全是 0。
                normal = [g for g in case.graders if g.name != "MetaEvaluator"]
                graders = build_evaluators(
                    [{"name": g.name, "config": g.config} for g in normal]
                )
                # **每个 case 一个 judge client**：它的 `judge_results` 是这次
                # case 的 judge 轨迹。共用一个的话，并发跑 8 条时各 case 的
                # judge run 会混在一起，元评测就会拿 A 的判定去算 B 的一致性。
                judge_client = self._build_judge_client(suite, store)
                ctx = EvalContext(judge=judge_client)
                evals = await run_evaluators(graders, result.trajectory, ctx)
                # 元评测跑在 **judge 自己的轨迹** 上 —— 这是双 Harness 对称的兑现点
                evals.extend(await self._meta_evals(case, judge_client, result))
                # 落索引 —— M8 的聚合报告从这里读回，不用重跑评测
                await store.put_evals(result.run_id, evals)
        finally:
            # 录制内容必须在退出前落盘 —— 否则录了个寂寞
            if isinstance(provider, RecordingProvider):
                provider.save()

        self._spent_usd += result.usage.cost_usd
        return RunOutcome(result=result, evals=evals,
                          case_id=case.case_id, repeat_index=repeat_index)

    # ---- judge ----
    def _build_judge_client(
        self, suite: Suite, store: CompositeStore
    ) -> RunBasedJudgeClient | None:
        """按 suite 的 `judge:` 块装配 judge。没配就返回 None。

        **judge 复用同一个 `Run` 类** —— 这正是"双 Harness 对称"的落点：
        差异只有 RunSpec 的取值（role / prompt / 工具白名单 / 预算）。
        这里不做任何 `JudgeRun` 之类的特化，那些特化会让"judge 判得准不准"
        变成无法回答的问题。
        """
        cfg = suite.judge_config()
        if cfg is None:
            return None

        judge_provider = self._build_provider(
            ModelRef(provider=cfg.provider, model=cfg.model),
            cfg.fake_script, role="judge")

        def factory(spec: RunSpec, subject: Trajectory | None) -> Run:
            return Run(spec, RunDeps(
                provider=judge_provider,
                # judge 的轨迹也进同一个 store：它要能被 trace、被元评测读回。
                # run_id 不同，不会与 sut 的轨迹混。
                store=store,
                tools=build_judge_tools(subject),
                middlewares=build_middlewares(spec.middlewares),
                workdir=self.workdir,
                id_gen=default_run_id,
            ))

        return RunBasedJudgeClient(factory, JudgeConfig(
            model=cfg.model, provider=cfg.provider, rubric=cfg.rubric,
            max_usd=cfg.max_usd, max_turns=cfg.max_turns,
        ))

    async def _meta_evals(
        self,
        case: CaseSpec,
        client: RunBasedJudgeClient | None,
        result: RunResult,
    ) -> list[EvalResult]:
        """触发 judge、然后在 **judge 自己的轨迹**上跑元评测。

        ## 谁触发 judge —— 计划里没写，这里定下来

        评测器只读轨迹、绝不起 agent run（那是「评测器不依赖 core」这条
        架构约束的实质）。所以**触发 judge 是组装层的事**：
        这里显式地判 N 次，再让 `MetaEvaluator` 去读那些轨迹。

        反过来做（让 MetaEvaluator 自己调 `ctx.judge`）会把
        "评测器触发 agent run" 这条反向依赖重新引进来，
        而那正是整个分层要避免的。

        ## 只对**声明需要**的 case 触发

        judge 是要花钱的。没在 `graders` 里写 `MetaEvaluator` 就不判 ——
        "配了 judge 就跑"会让没要它的用例也付钱。
        """
        wants = [g for g in case.graders if g.name == "MetaEvaluator"]
        if client is None or not wants:
            return []

        cfg = wants[0].config or {}
        repeat = int(cfg.get("judge_repeat", 1))

        # 只取**这一轮元评测自己产生**的 judge run：FailureClassifier 的
        # LLM 兜底可能已经往里塞过若干条，混进来会把两批判定的重复次数
        # 搅在一起，一致性就算错了。
        start = len(client.judge_results)
        await client.judge(
            JudgeCase(
                case_id=case.case_id,
                task=case.task.prompt,
                traj=result.trajectory,
                rubric=str(cfg.get("rubric", "")),
            ),
            repeat=repeat,
        )
        judge_runs = client.judge_results[start:]
        if not judge_runs:
            return []

        meta_traj = meta_trajectory(judge_runs, run_id=f"{case.case_id}-meta")
        graders = build_evaluators([{"name": "MetaEvaluator", "config": cfg}])
        # 元评测读的是 judge 轨迹，**不再注入 judge** —— 注入的话
        # "评测评判官的判官"这个递归就打开了，而 MAX_DEPTH=1 明确禁止。
        return await run_evaluators(graders, meta_traj, EvalContext())

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
