"""OTel JSONL 轨迹源适配器测试。

## 通用性论证的落点

Harness 不只评测自研 agent：任何能产出 OTel GenAI 风格轨迹的系统都能接进来。
这是「过程级评测」这件事作为**平台**而非**脚本**的证据。

## 这里最重要的一类测试是往返

`trajectory → spans → JSONL → trajectory` 必须把评测器依赖的字段全部留住。
单向测试只能证明"投影没崩"，往返测试才能证明**投影与导入对同一件事的理解一致** ——
两者各自"看起来对"却互相不兼容，是这个模块最可能的失败方式。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.adapters.base import TrajectorySource
from harness.adapters.otel_jsonl import OtelJsonlSource
from harness.events.otel import trajectory_to_otlp
from harness.events.trajectory import Trajectory
from harness.events.types import (
    EventType,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from harness.testing.builder import TrajectoryBuilder as TB


def _traj() -> Trajectory:
    return (TB(run_id="r1", task="fix it", role="sut", model="deepseek-flash")
            .turn()
            .llm_response(text="let me look",
                          tool_calls=[("read_file", {"path": "a.py"})],
                          input_tokens=730, output_tokens=81, cost_usd=0.0001)
            .tool_result(name="read_file", content="x = 1", ok=True)
            .turn()
            .llm_response(text="done", input_tokens=800, output_tokens=20,
                          cost_usd=0.0002)
            .run_end(status="ok", input_tokens=1530, output_tokens=101,
                     final_output="done")
            .build())


def _dump(traj: Trajectory, path: Path) -> Path:
    spans = trajectory_to_otlp(traj)
    path.write_text("\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def projected(tmp_path: Path) -> Path:
    return _dump(_traj(), tmp_path / "r1.jsonl")


# ---- 协议一致性 ----
def test_the_source_satisfies_the_protocol():
    """适配器满足 `TrajectorySource` 协议。

    `runtime_checkable` 的 Protocol 只能校验方法名 —— 但它至少保证
    组装层可以按协议编程（`RunDeps.source` 就是这么用的）。
    """
    assert isinstance(OtelJsonlSource(), TrajectorySource)


def test_the_source_declares_a_name():
    assert OtelJsonlSource().name == "otel_jsonl"


# ---- can_load ----
def test_can_load_accepts_a_projected_file(projected: Path):
    assert OtelJsonlSource().can_load(str(projected)) is True


def test_can_load_rejects_a_missing_path(tmp_path: Path):
    assert OtelJsonlSource().can_load(str(tmp_path / "nope.jsonl")) is False


def test_can_load_rejects_a_non_jsonl_suffix(tmp_path: Path):
    p = tmp_path / "trace.json"
    p.write_text("{}\n", encoding="utf-8")
    assert OtelJsonlSource().can_load(str(p)) is False


def test_can_load_rejects_a_jsonl_that_is_not_otel(tmp_path: Path):
    """★ 后缀对了不代表内容对了。

    只看后缀的话，任何 `.jsonl` 都会被这个 adapter 认领 ——
    包括本 harness 自己写的轨迹文件。那会让人以为"导入成功了"，
    实际拿到一条空轨迹。
    """
    p = tmp_path / "other.jsonl"
    p.write_text('{"type": "run.start", "run_id": "x"}\n', encoding="utf-8")
    assert OtelJsonlSource().can_load(str(p)) is False


def test_can_load_rejects_an_empty_file(tmp_path: Path):
    p = tmp_path / "empty.jsonl"
    p.write_text("\n\n", encoding="utf-8")
    assert OtelJsonlSource().can_load(str(p)) is False


# ---- load：往返 ----
async def test_roundtrip_preserves_run_id(projected: Path):
    """run_id 来自 `gen_ai.conversation.id` 而**不是文件名**。

    取自文件名会在"把轨迹复制成 trace-1.jsonl"时静默改名，
    而报告里所有对 run 的深链都会指错。
    """
    traj = await OtelJsonlSource().load(str(projected))
    assert traj.run_id == "r1"


async def test_roundtrip_preserves_the_tool_sequence(projected: Path):
    traj = await OtelJsonlSource().load(str(projected))
    assert traj.tool_sequence() == ("read_file",)


async def test_roundtrip_preserves_tool_result_content(projected: Path):
    """GroundingChecker 靠它工作 —— 丢了就没法做 grounding 评测。"""
    traj = await OtelJsonlSource().load(str(projected))
    call = traj.tool_calls()[0]
    result = traj.result_for(call.call_id)
    assert result is not None
    assert result.content == "x = 1"
    assert result.ok is True


async def test_roundtrip_preserves_tokens_and_cost(projected: Path):
    """★ 没有 token 与成本，"效率分析"就全是 0 —— 而 0 看起来像"很省"。"""
    traj = await OtelJsonlSource().load(str(projected))
    assert traj.input_tokens == 1530
    assert traj.output_tokens == 101
    assert traj.cost_usd == pytest.approx(0.0003)


async def test_roundtrip_preserves_llm_response_count(projected: Path):
    traj = await OtelJsonlSource().load(str(projected))
    assert len(traj.llm_responses()) == 2


async def test_roundtrip_preserves_assistant_text(projected: Path):
    """助手文本是 GroundingChecker 的另一半输入。"""
    traj = await OtelJsonlSource().load(str(projected))
    assert [r.text for r in traj.llm_responses()] == ["let me look", "done"]


async def test_roundtrip_preserves_tool_call_arguments(projected: Path):
    """参数必须留住 —— 否则 `TrajectoryMatcher` 的 tool_args 维度无法评测。"""
    traj = await OtelJsonlSource().load(str(projected))
    assert traj.tool_calls()[0].arguments == {"path": "a.py"}


async def test_roundtrip_preserves_failure_metadata(tmp_path: Path):
    """失败的工具导入后仍要是失败的，且错误类型要留住。

    全部导入成"成功"会让导入的轨迹永远是满分 —— 那比不导入更糟。
    """
    traj = (TB(run_id="r2")
            .turn()
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest"]})])
            .tool_result(name="run_command", content="boom", ok=False,
                         error="exit 1", error_type="nonzero_exit", truncated=True)
            .run_end(status="ok")
            .build())
    loaded = await OtelJsonlSource().load(str(_dump(traj, tmp_path / "r2.jsonl")))
    result = loaded.result_for(loaded.tool_calls()[0].call_id)
    assert result is not None
    assert result.ok is False
    assert result.error_type == "nonzero_exit"
    assert result.truncated is True


async def test_roundtrip_preserves_a_policy_denial(tmp_path: Path):
    """★ 往返要留住"被中间件拦下"这个区分。

    报告里"被策略拦下"与"工具执行失败"分开统计 ——
    全压成 `ok=False` 之后，一个安全边界配置过严的 run
    看起来会和模型不会用工具长得一模一样。
    """
    traj = Trajectory.from_events("r3", [
        RunStartEvent(run_id="r3", seq=0, type=EventType.RUN_START,
                      role="sut", model="m", provider="p"),
        ToolCallEvent(run_id="r3", seq=1, type=EventType.TOOL_CALL,
                      call_id="c1", name="run_command"),
        ToolResultEvent(run_id="r3", seq=2, type=EventType.TOOL_RESULT,
                        call_id="c1", name="run_command", ok=False,
                        denied_by="policy", error_type="denied"),
        RunEndEvent(run_id="r3", seq=3, type=EventType.RUN_END, status="ok"),
    ])
    loaded = await OtelJsonlSource().load(str(_dump(traj, tmp_path / "r3.jsonl")))
    result = loaded.result_for("c1")
    assert result is not None
    assert result.ok is False
    assert result.denied_by == "policy"


async def test_the_loaded_trajectory_ends_with_an_imported_run_end(projected: Path):
    """导入的轨迹必须有一条 `RunEndEvent`。

    一大堆评测器读 `traj.status`，缺了它就是 `None`，
    而 `None` 会让它们静默跳过而不是报错。
    """
    traj = await OtelJsonlSource().load(str(projected))
    assert traj.end() is not None
    assert traj.status == "imported"


# ---- 兼容第三方产出 ----
async def test_reads_the_legacy_token_attribute_names(tmp_path: Path):
    """★ 别人（真第三方）发的多半还是旧名。

    只认新名的话，导入别人的轨迹会得到全 0 的 token —— 静默且错误。
    """
    p = tmp_path / "legacy.jsonl"
    p.write_text(json.dumps({
        "name": "invoke_agent my-agent",
        "attributes": {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.system": "openai",              # 旧名，只有它
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.prompt_tokens": 500,      # 旧名
            "gen_ai.usage.completion_tokens": 50,   # 旧名
        },
    }) + "\n", encoding="utf-8")

    traj = await OtelJsonlSource().load(str(p))
    assert traj.input_tokens == 500
    assert traj.output_tokens == 50


async def test_agent_name_and_model_are_read_from_a_third_party_span(tmp_path: Path):
    p = tmp_path / "third.jsonl"
    p.write_text(json.dumps({
        "attributes": {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "claude-sonnet-5",
            "gen_ai.agent.name": "langgraph-agent",
            "gen_ai.conversation.id": "thread-42",
            "gen_ai.usage.input_tokens": 10,
        },
    }) + "\n", encoding="utf-8")

    traj = await OtelJsonlSource().load(str(p))
    assert traj.run_id == "thread-42"
    start = traj.start()
    assert start is not None
    assert start.role == "external"          # 导入的一律是 external 角色
    assert start.model == "claude-sonnet-5"
    assert start.provider == "anthropic"


async def test_a_tool_span_without_a_result_is_imported_as_a_dangling_call(tmp_path: Path):
    """缺 result 的工具 span 导入成悬空配对。

    真实的第三方轨迹**经常**是这样（进程被杀、只发了 call 就断了）。
    不能因为缺 result 就整行丢掉 —— 丢掉会让失败看起来像"没调用过"。
    """
    p = tmp_path / "dangling.jsonl"
    p.write_text("\n".join([
        json.dumps({"attributes": {"gen_ai.operation.name": "invoke_agent",
                                   "gen_ai.conversation.id": "x"}}),
        json.dumps({"attributes": {"gen_ai.operation.name": "execute_tool",
                                   "gen_ai.tool.name": "read_file",
                                   "gen_ai.tool.call.id": "c1"}}),
    ]) + "\n", encoding="utf-8")

    traj = await OtelJsonlSource().load(str(p))
    assert traj.tool_sequence() == ("read_file",)
    assert traj.result_for("c1") is None


async def test_missing_tool_ids_get_deterministic_synthetic_ids(tmp_path: Path):
    """缺 call.id 时用文件内位置生成 id。

    **必须确定性** —— 否则同一条轨迹导入两次得到不同的 call_id，评测结果会抖。
    """
    line = json.dumps({"attributes": {"gen_ai.operation.name": "execute_tool",
                                      "gen_ai.tool.name": "read_file"}})
    p = tmp_path / "noids.jsonl"
    p.write_text(f"{line}\n{line}\n", encoding="utf-8")

    src = OtelJsonlSource()
    a = await src.load(str(p))
    b = await src.load(str(p))
    assert [c.call_id for c in a.tool_calls()] == [c.call_id for c in b.tool_calls()]
    assert len({c.call_id for c in a.tool_calls()}) == 2  # 且互不相同


# ---- 退化输入 ----
async def test_a_file_without_an_invoke_agent_span_still_loads(tmp_path: Path):
    """只有工具 span 的片段也要能导入（文件名兜底做 run_id）。"""
    p = tmp_path / "frag.jsonl"
    p.write_text(json.dumps({
        "attributes": {"gen_ai.operation.name": "execute_tool",
                       "gen_ai.tool.name": "list_dir"},
    }) + "\n", encoding="utf-8")

    traj = await OtelJsonlSource().load(str(p))
    assert traj.run_id == "frag"
    assert traj.tool_sequence() == ("list_dir",)
    assert traj.start() is not None       # 补一个，否则一堆评测器拿不到 model


async def test_unknown_operations_are_skipped_not_fatal(tmp_path: Path):
    """第三方轨迹里会有 `chat`/`embeddings`/自定义 span —— 不认识的跳过即可。

    为一条不认识的 span 崩掉整个导入，等于宣布"只支持我们自己的导出格式"，
    通用性就没了。
    """
    p = tmp_path / "mixed.jsonl"
    p.write_text("\n".join([
        json.dumps({"attributes": {"gen_ai.operation.name": "embeddings"}}),
        json.dumps({"attributes": {"gen_ai.operation.name": "invoke_agent",
                                   "gen_ai.conversation.id": "m"}}),
        json.dumps({"attributes": {"gen_ai.operation.name": "execute_tool",
                                   "gen_ai.tool.name": "read_file"}}),
    ]) + "\n", encoding="utf-8")

    traj = await OtelJsonlSource().load(str(p))
    assert traj.tool_sequence() == ("read_file",)


async def test_blank_lines_are_ignored(tmp_path: Path):
    p = tmp_path / "blank.jsonl"
    p.write_text('\n\n{"attributes": {"gen_ai.operation.name": "invoke_agent",'
                 ' "gen_ai.conversation.id": "b"}}\n\n', encoding="utf-8")
    traj = await OtelJsonlSource().load(str(p))
    assert traj.run_id == "b"


async def test_malformed_json_names_the_line_number(tmp_path: Path):
    """★ 坏数据要**报错且指明位置**，不能静默跳过。

    静默跳过坏行会得到"少了几个工具调用"的轨迹 ——
    评测器会把它当成 agent 没做那几步，而不是数据坏了。
    指向行号才让人能在 10 分钟内修好。
    """
    p = tmp_path / "bad.jsonl"
    p.write_text('{"attributes": {}}\nnot json at all\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        await OtelJsonlSource().load(str(p))


async def test_imported_events_are_a_valid_trajectory(projected: Path):
    """导入结果必须能当普通轨迹用：JSONL 往返 + 事件类型齐全。"""
    traj = await OtelJsonlSource().load(str(projected))
    reparsed = Trajectory.from_jsonl(traj.to_jsonl())
    assert reparsed.run_id == traj.run_id
    assert reparsed.tool_sequence() == traj.tool_sequence()
    assert len(reparsed.of(EventType.LLM_RESPONSE)) == 2
