"""装配层：把 suite 配置翻译成 `RunSpec` + `RunDeps`。

## 本模块是 M1 的最小版

只支持 fake provider + 单条任务。完整装配（中间件工厂、judge runner、
评测器调度、并发 suite）在**任务 27** 补齐。

之所以现在就建：`cli.py` 需要一个组装点，而"先立接口、后补实现"
可以让任务 27 只改这一个文件，不用动 CLI。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from harness.contracts.protocols import LLMResponse
from harness.contracts.spec import Budget, ModelRef, RunRole, RunSpec, ToolPolicy
from harness.core.registry import ToolRegistry
from harness.core.run import Run, RunDeps, RunResult
from harness.core.tools.finish import FinishTool
from harness.providers.fake import FakeProvider, text_response, tool_call_response
from harness.store.jsonl import JsonlStore


class SuiteConfigError(ValueError):
    """suite 文件格式错误 —— 映射到 CLI 退出码 2。"""


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


class RunBuilder:
    """最小装配层。任务 27 会扩展为支持并发 suite、middleware 工厂与评测调度。"""

    def __init__(self, *, out_dir: Path | str = Path("runs")) -> None:
        self.out_dir = Path(out_dir)

    def run_suite_sync(self, suite_path: Path | str) -> list[RunResult]:
        return asyncio.run(self.run_suite(suite_path))

    async def run_suite(self, suite_path: Path | str) -> list[RunResult]:
        cfg = load_suite_config(suite_path)

        tools = ToolRegistry()
        tools.register(FinishTool())

        spec = RunSpec(
            role=RunRole.SUT,
            system_prompt=str(cfg.get("system_prompt") or "You are a careful agent."),
            model=ModelRef(provider="fake", model="fake"),
            tools=ToolPolicy(),
            budget=Budget(max_turns=int(cfg.get("max_turns") or 5)),
        )

        store = JsonlStore(self.out_dir)
        deps = RunDeps(
            provider=FakeProvider(build_fake_script(cfg.get("fake_script"))),
            store=store,
            tools=tools,
        )
        result = await Run(spec, deps).execute()
        await store.close()
        return [result]


def dumps(obj: Any) -> str:
    """统一的 JSON 输出格式 —— 保证中文不被转义、key 有序。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
