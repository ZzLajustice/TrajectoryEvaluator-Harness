"""装配层：把 suite 配置翻译成 `RunSpec` + `RunDeps`。

## 当前覆盖

  ✓ fake provider + record/replay 包装
  ✓ 完整 SUT 工具集（6 个）
  ✓ 中间件工厂（按规范顺序构造）
  ✓ 任务提示词装配

## 仍待补齐（任务 27）

  并发 suite、多 case 调度、评测器调度、judge runner

之所以先建这个组装点：`cli.py` 只需要一个入口，
后续扩展集中在本文件，不用动 CLI。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from harness.contracts.protocols import EvalContext, LLMResponse
from harness.contracts.results import EvalResult
from harness.contracts.spec import (
    Budget,
    MiddlewareSpec,
    ModelRef,
    RunRole,
    RunSpec,
    TaskSpec,
    ToolPolicy,
    WorkspaceSpec,
)
from harness.core.middleware.factory import build_middlewares
from harness.core.registry import ToolRegistry
from harness.core.run import Run, RunDeps, RunResult
from harness.core.tools.finish import FinishTool
from harness.core.tools.fs import ListDirTool, ReadFileTool, WriteFileTool
from harness.core.tools.search import SearchTool
from harness.core.tools.shell import RunCommandTool
from harness.evaluators.base import run_evaluators
from harness.orchestration.evalrunner import build_evaluators
from harness.providers.fake import FakeProvider, text_response, tool_call_response
from harness.providers.recording import RecordingProvider, ReplayProvider
from harness.providers.response_pool import ResponsePool
from harness.store.jsonl import JsonlStore


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


class SuiteConfigError(ValueError):
    """suite 文件格式错误 —— 映射到 CLI 退出码 2。"""


_WORKSPACE_KINDS = ("copy", "git_worktree", "tempdir")


def _workspace_kind(raw: Any) -> Any:
    """校验并收窄 workspace kind。

    拼错的值必须报错 —— 静默回退到默认会让"我明明配了 copy"变成
    "为什么工作目录是空的"这类难查的问题。
    """
    if raw is None:
        return "tempdir"
    kind = str(raw)
    if kind not in _WORKSPACE_KINDS:
        raise SuiteConfigError(
            f"unknown workspace kind {kind!r}; known: {list(_WORKSPACE_KINDS)}"
        )
    return kind


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


def load_suite_config(suite_path: Path | str) -> dict[str, Any]:
    path = Path(suite_path)
    if not path.exists():
        raise FileNotFoundError(f"suite not found: {path}")
    # 只用 safe_load —— 绝不执行 YAML 里的任意 Python 对象
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SuiteConfigError(f"suite root must be a mapping, got {type(raw).__name__}")
    return raw


@dataclass(slots=True)
class RunOutcome:
    """一次 run 及其评测结果。

    合并成一个对象返回，而不是让调用方自己配对 —— 配对错了（比如结果错位）
    会让报告张冠李戴，且不会有任何报错。
    """

    result: RunResult
    evals: list[EvalResult] = field(default_factory=list)


class RunBuilder:
    """装配层。任务 27 会扩展为支持并发 suite 与评测调度。"""

    def __init__(
        self,
        *,
        out_dir: Path | str = Path("runs"),
        record: Path | str | None = None,
        replay: Path | str | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.record = Path(record) if record else None
        self.replay = Path(replay) if replay else None

    def _build_provider(self, cfg: dict[str, Any]) -> Any:
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

        base = FakeProvider(build_fake_script(cfg.get("fake_script")))
        if self.record is not None:
            return RecordingProvider(base, ResponsePool(self.record))
        return base

    def run_suite_sync(
        self, suite_path: Path | str, *, evaluate: bool = False
    ) -> list[RunOutcome]:
        return asyncio.run(self.run_suite(suite_path, evaluate=evaluate))

    async def run_suite(
        self, suite_path: Path | str, *, evaluate: bool = False
    ) -> list[RunOutcome]:
        cfg = load_suite_config(suite_path)

        tools = build_tool_registry()

        spec = RunSpec(
            role=RunRole.SUT,
            system_prompt=str(cfg.get("system_prompt") or "You are a careful agent."),
            model=ModelRef(provider="fake", model="fake"),
            task=TaskSpec(case_id=str(cfg.get("name", "case")),
                          prompt=str(cfg.get("task") or "Say hello and finish.")),
            tools=ToolPolicy(),
            budget=Budget(max_turns=int(cfg.get("max_turns") or 5)),
            # 默认给一个临时工作目录 —— SUT 是代码修复类 agent，
            # 没有工作目录时文件工具全部失败（实测踩过：ctx.ws 为 None，
            # 工具报 AttributeError，沙箱中间件也因拿不到 root 而静默放行越狱）
            workspace=WorkspaceSpec(
                kind=_workspace_kind(cfg.get("workspace")),
                source=cfg.get("workspace_source"),
            ),
        )

        store = JsonlStore(self.out_dir)
        provider = self._build_provider(cfg)
        # 中间件由工厂按**规范顺序**构造 —— suite 只决定启用哪些，不决定顺序
        middlewares = build_middlewares(
            [MiddlewareSpec(**m) if isinstance(m, dict) else MiddlewareSpec(name=str(m))
             for m in (cfg.get("middlewares") or [])]
        )
        deps = RunDeps(provider=provider, store=store, tools=tools,
                       middlewares=middlewares)

        evals: list[EvalResult] = []
        try:
            result = await Run(spec, deps).execute()
            if evaluate:
                # 评测器在轨迹落盘之后跑 —— 它只读轨迹，与 agent 零耦合。
                # 只读是「评测器绝不 import core」这条架构约束的运行时体现：
                # 若哪天评测器想直接驱动 agent，这里就拿不到任何句柄。
                graders = build_evaluators(cfg.get("graders") or [])
                evals = await run_evaluators(graders, result.trajectory, EvalContext())
        finally:
            await store.close()
            # 录制内容必须在退出前落盘 —— 否则录了个寂寞
            if isinstance(provider, RecordingProvider):
                provider.save()

        return [RunOutcome(result=result, evals=evals)]


def dumps(obj: Any) -> str:
    """统一的 JSON 输出格式 —— 保证中文不被转义、key 有序。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
