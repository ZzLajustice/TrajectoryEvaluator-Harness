"""OTel GenAI 投影层测试。

## 这个模块存在的唯一理由

`gen_ai.*` 属性**没有一个达到 Stable**，规范原文写着 "SHOULD NOT be used in
production"、"MAY be removed without prior notice"，且 2026-06 已从核心 semconv
迁到独立的 semantic-conventions-genai，属性名还在改。

所以本项目的策略是：**内部字段名是稳定契约，OTel 命名只活在一个文件里**。
下面的测试就是这句话的可执行版本。
"""

from __future__ import annotations

import json

from harness.events.otel import SEMCONV_VERSION, trajectory_to_otlp
from harness.events.trajectory import Trajectory
from harness.events.types import (
    EventType,
    LLMResponseEvent,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from harness.testing.builder import TrajectoryBuilder as TB


def _traj() -> Trajectory:
    """一条含 1 次 LLM 调用 + 1 次成功工具调用的轨迹。"""
    return (TB(run_id="r1", task="fix it", role="sut", model="deepseek-flash")
            .turn()
            .llm_response(text="let me look",
                          tool_calls=[("read_file", {"path": "a.py"})],
                          input_tokens=730, output_tokens=81)
            .tool_result(name="read_file", content="x = 1", ok=True)
            .turn()
            .llm_response(text="done", input_tokens=800, output_tokens=20)
            .run_end(status="ok", input_tokens=1530, output_tokens=101)
            .build())


def _attrs(span: dict) -> dict:
    return span["attributes"]


def _root(traj: Trajectory) -> dict:
    return trajectory_to_otlp(traj)[0]


# ---- 隔离：OTel 命名不得渗进事件模型 ----
def test_projection_never_leaks_otel_names_into_event_model():
    """★ OTel 命名只活在一个文件里 —— 事件模型不得出现 `gen_ai.*` 字段。

    这条测试防的是"顺手给事件加个 gen_ai 字段"。一旦发生，
    规范的改名就会变成事件 schema 改动，而事件 schema 是本项目最贵的改动
    （已提交的老轨迹必须永远可读）。
    """
    from harness.events import types as T

    for obj in vars(T).values():
        if isinstance(obj, type) and hasattr(obj, "model_fields"):
            for field in obj.model_fields:
                assert not field.startswith("gen_ai."), f"{obj.__name__}.{field}"


def test_the_word_gen_ai_appears_only_in_the_projection_module():
    """连**字符串**也只准出现在 otel.py 里。

    字段名那条管不到 `attrs` 逃生舱里的字面量 —— 而把 `gen_ai.tool.name`
    硬编码进 core 会让投影层名存实亡。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "harness"
    # 投影层（导出）与 otel adapter（导入）是这道边界的**两侧**，都允许出现。
    allowed = {"events/otel.py", "adapters/otel_jsonl.py"}
    offenders = []
    for py in sorted(src.rglob("*.py")):
        rel = py.relative_to(src).as_posix()
        if rel in allowed:
            continue
        if "gen_ai." in py.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, (
        f"`gen_ai.` 字面量只允许出现在 {sorted(allowed)}：\n  " + "\n  ".join(offenders))


# ---- 根 span ----
def test_root_span_is_an_invoke_agent_span():
    root = _root(_traj())
    assert _attrs(root)["gen_ai.operation.name"] == "invoke_agent"
    assert root["name"].startswith("invoke_agent")


def test_root_span_carries_identity_and_model():
    a = _attrs(_root(_traj()))
    assert a["gen_ai.conversation.id"] == "r1"
    assert a["gen_ai.agent.name"] == "sut"
    assert a["gen_ai.request.model"] == "deepseek-flash"
    assert a["semconv.version"] == SEMCONV_VERSION


def test_deprecated_gen_ai_system_is_not_emitted():
    """`gen_ai.system` 已被 `gen_ai.provider.name` 取代 —— 新代码不该再发旧名。"""
    a = _attrs(_root(_traj()))
    assert "gen_ai.system" not in a
    assert a["gen_ai.provider.name"]


def test_dual_emits_old_and_new_token_names():
    """★ 新旧 token 属性名同时输出，一个 semconv 周期后再去掉 legacy。

    只发一套的代价是：对接任何一端（按旧名读的 / 按新名读的）都会静默拿到 0，
    而 0 个 token 看起来只是"这次调用很短"。
    """
    a = _attrs(_root(_traj()))
    assert a["gen_ai.usage.input_tokens"] == 1530
    assert a["gen_ai.usage.output_tokens"] == 101
    assert a["gen_ai.usage.prompt_tokens"] == 1530       # legacy
    assert a["gen_ai.usage.completion_tokens"] == 101    # legacy


# ---- per-call span ----
def test_each_llm_call_becomes_a_chat_span_with_its_own_usage():
    """按**每次调用**而不是累计值输出 —— 只有这样才能看出一轮多少钱。"""
    spans = trajectory_to_otlp(_traj())
    chats = [s for s in spans if _attrs(s)["gen_ai.operation.name"] == "chat"]
    assert len(chats) == 2
    assert _attrs(chats[0])["gen_ai.usage.input_tokens"] == 730
    assert _attrs(chats[1])["gen_ai.usage.input_tokens"] == 800


def test_each_tool_call_becomes_an_execute_tool_span():
    spans = trajectory_to_otlp(_traj())
    tools = [s for s in spans if _attrs(s)["gen_ai.operation.name"] == "execute_tool"]
    assert len(tools) == 1
    assert _attrs(tools[0])["gen_ai.tool.name"] == "read_file"
    assert _attrs(tools[0])["gen_ai.tool.call.id"]


def test_tool_result_content_is_projected():
    """★ 结果原文必须带上 —— 丢了它，导入的轨迹无法做 grounding 评测。

    OTel 里内容捕获是 opt-in 的（隐私考虑），但评测 harness 的场景下
    没有原文就等于没有评测。
    """
    spans = trajectory_to_otlp(_traj())
    tool = next(s for s in spans if _attrs(s)["gen_ai.operation.name"] == "execute_tool")
    assert _attrs(tool)["gen_ai.tool.call.result"] == "x = 1"


def test_a_failed_tool_sets_otel_error_status():
    traj = (TB(run_id="r1")
            .turn()
            .llm_response(tool_calls=[("read_file", {"path": "missing.py"})])
            .tool_result(name="read_file", content="", ok=False,
                         error="no such file", error_type="not_found")
            .run_end(status="ok")
            .build())
    tool = next(s for s in trajectory_to_otlp(traj)
                if _attrs(s)["gen_ai.operation.name"] == "execute_tool")
    assert tool["status"]["code"] == "ERROR"
    assert _attrs(tool)["error.type"] == "not_found"


def test_a_denied_tool_names_the_middleware_that_denied_it():
    """被策略拦下与真的执行失败是两回事 —— 报告里要能区分。"""
    traj = Trajectory.from_events("r1", [
        RunStartEvent(run_id="r1", seq=0, type=EventType.RUN_START,
                      role="sut", model="m", provider="p"),
        ToolCallEvent(run_id="r1", seq=1, type=EventType.TOOL_CALL,
                      call_id="c1", name="run_command", arguments={"argv": ["rm", "-rf", "/"]}),
        ToolResultEvent(run_id="r1", seq=2, type=EventType.TOOL_RESULT,
                        call_id="c1", name="run_command", ok=False,
                        denied_by="policy", error_type="denied"),
        RunEndEvent(run_id="r1", seq=3, type=EventType.RUN_END, status="ok"),
    ])
    tool = next(s for s in trajectory_to_otlp(traj)
                if _attrs(s)["gen_ai.operation.name"] == "execute_tool")
    assert _attrs(tool)["error.type"] == "policy_denied:policy"


def test_truncated_tool_output_is_flagged():
    """★ 截断标记必须投影出去。

    `trap_fabricate` 的整个考点是"输出被截断了，agent 却声称全部通过"。
    导入的轨迹若不带这个标记，GroundingChecker 会把截断当完整输出，
    于是**无法区分"没看到失败"与"没看"**。
    """
    traj = (TB(run_id="r1")
            .turn()
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest"]})])
            .tool_result(name="run_command", content="1 failed... [truncated]",
                         ok=True, truncated=True)
            .run_end(status="ok")
            .build())
    tool = next(s for s in trajectory_to_otlp(traj)
                if _attrs(s)["gen_ai.operation.name"] == "execute_tool")
    assert _attrs(tool)["gen_ai.tool.call.result.truncated"] is True


def test_budget_exceeded_becomes_an_error_type():
    traj = (TB(run_id="r1").run_end(status="budget_exceeded").build())
    assert _attrs(_root(traj))["error.type"] == "budget_exceeded"


# ---- 退化输入 ----
def test_an_empty_trajectory_projects_to_an_empty_list():
    """空轨迹投影成空列表，而不是抛异常。

    乱序/截断的轨迹不能把导出炸掉 —— 导出是偶尔才跑一次的路径，
    炸了往往没人发现，直到答辩前想起要导一次。
    """
    assert trajectory_to_otlp(Trajectory.from_events("r1", [])) == []


def test_a_trajectory_without_run_end_still_projects():
    traj = Trajectory.from_events("r1", [
        RunStartEvent(run_id="r1", seq=0, type=EventType.RUN_START,
                      role="sut", model="m", provider="p"),
    ])
    spans = trajectory_to_otlp(traj)
    assert len(spans) == 1
    assert "error.type" not in _attrs(spans[0])


def test_an_orphan_tool_call_projects_the_call_without_a_result():
    """悬空配对（有 call 无 result）导出时必须不崩 —— 它是被截断的轨迹的常态。"""
    traj = Trajectory.from_events("r1", [
        RunStartEvent(run_id="r1", seq=0, type=EventType.RUN_START,
                      role="sut", model="m", provider="p"),
        ToolCallEvent(run_id="r1", seq=1, type=EventType.TOOL_CALL,
                      call_id="c1", name="read_file"),
    ])
    tool = next(s for s in trajectory_to_otlp(traj)
                if _attrs(s)["gen_ai.operation.name"] == "execute_tool")
    assert "gen_ai.tool.call.result" not in _attrs(tool)


# ---- 不依赖 OTel SDK ----
def test_projection_output_is_plain_json_serialisable_dicts():
    """★ OTel SDK 在 `[otel]` extra 里，**不进核心依赖**。

    所以投影层只产出普通 dict，且必须能直接 `json.dumps` ——
    （真实 span 对象带不可序列化的字段，那正是不能用 SDK 类型做接口的原因）。
    """
    spans = trajectory_to_otlp(_traj())
    assert all(isinstance(s, dict) for s in spans)
    json.dumps(spans)  # 不抛异常即通过


def test_every_span_has_a_name_and_an_operation():
    for span in trajectory_to_otlp(_traj()):
        assert span["name"]
        assert _attrs(span)["gen_ai.operation.name"]


def test_llm_response_text_survives():
    """助手文本是 GroundingChecker 的输入 —— 投影不能把它扔掉。

    GenAI semconv 的内容捕获目前是 event 形式的（不稳定），
    所以这里用厂商前缀属性承载，见 otel.py 的说明。
    """
    spans = trajectory_to_otlp(_traj())
    chat = next(s for s in spans if _attrs(s)["gen_ai.operation.name"] == "chat")
    assert _attrs(chat)["harness.llm.text"] == "let me look"


def test_cost_is_projected_under_a_vendor_prefix():
    """成本是本项目的一等指标，而 semconv 没有对应属性 —— 用厂商前缀承载。"""
    traj = Trajectory.from_events("r1", [
        RunStartEvent(run_id="r1", seq=0, type=EventType.RUN_START,
                      role="sut", model="m", provider="p"),
        LLMResponseEvent(run_id="r1", seq=1, type=EventType.LLM_RESPONSE,
                         turn=0, model="m", cost_usd=0.0001234),
    ])
    chat = next(s for s in trajectory_to_otlp(traj)
                if _attrs(s)["gen_ai.operation.name"] == "chat")
    assert _attrs(chat)["harness.cost_usd"] == 0.0001234
