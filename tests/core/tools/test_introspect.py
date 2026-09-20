"""自省工具测试。

## 为什么这些工具存在

Agent-as-a-Judge 相对"把整条轨迹塞进 prompt 让 LLM 一次性打分"的优势，
**全在于 judge 能自己去查**。没有 `read_trajectory`，所谓 judge agent
就只是一个带壳的单次调用 —— 对称性架构也就白搭了。

## 主体轨迹是**构造时注入**的，不是从 workspace 上取的

计划里写的是 `getattr(ws, "trajectory", None)`。改成构造注入有实质差别：
从 ws 上取的话，忘了绑定就会**静默**返回 "not_available"，
看起来像工具本身报错；构造注入下没绑就是构造参数缺失，在装配点就炸了。
"""

from __future__ import annotations

from harness.contracts.protocols import ToolCall
from harness.core.tools.introspect import ReadTrajectoryTool
from harness.testing import TrajectoryBuilder as TB


def _subject():
    return (TB(run_id="sut1").turn()
            .llm_response(text="looking", tool_calls=[("read_file", {"path": "a.py"})])
            .tool_result(name="read_file", content="code", ok=True)
            .turn()
            .llm_response(tool_calls=[("finish", {"summary": "done"})])
            .tool_result(name="finish", content="done", ok=True)
            .run_end(status="ok").build())


def _call(**args) -> ToolCall:
    return ToolCall(call_id="c1", name="read_trajectory", arguments=args)


async def test_it_renders_the_whole_trajectory_by_default():
    tool = ReadTrajectoryTool(_subject())
    r = await tool.invoke(_call(), None)
    assert r.ok
    assert "run.start" in r.content
    assert "tool.call" in r.content
    assert "run.end" in r.content


async def test_it_renders_a_slice_on_request():
    """Judge 需要能按需取片段 —— 整条塞进去会撑爆上下文。"""
    tool = ReadTrajectoryTool(_subject())
    full = await tool.invoke(_call(), None)
    part = await tool.invoke(_call(start=0, end=2), None)
    assert len(part.content.splitlines()) == 2
    assert len(part.content) < len(full.content)


async def test_slice_lines_carry_seq_and_type():
    tool = ReadTrajectoryTool(_subject())
    r = await tool.invoke(_call(start=0, end=1), None)
    line = r.content.splitlines()[0]
    assert line.strip().startswith("0")
    assert "run.start" in line


async def test_out_of_range_slice_is_clamped_not_fatal():
    """Judge 猜错范围是常事，不该让它变成一次工具失败。"""
    tool = ReadTrajectoryTool(_subject())
    r = await tool.invoke(_call(start=999, end=1000), None)
    assert r.ok
    assert r.content == "(empty)"


async def test_reversed_range_does_not_explode():
    tool = ReadTrajectoryTool(_subject())
    r = await tool.invoke(_call(start=5, end=1), None)
    assert r.ok


async def test_non_integer_arguments_are_rejected_cleanly():
    """LLM 给的参数是字符串是常态 —— 转不动就报清楚，别抛异常。"""
    tool = ReadTrajectoryTool(_subject())
    r = await tool.invoke(_call(start="abc"), None)
    assert not r.ok
    assert r.error_type == "bad_arguments"


async def test_an_empty_trajectory_is_reported_as_empty_not_an_error():
    # 注意：`TB().build()` **不是**空轨迹 —— builder 会自动补 run.start。
    # 真正的空轨迹要显式构造。
    from harness.events.trajectory import Trajectory

    tool = ReadTrajectoryTool(Trajectory.from_events("empty", []))
    r = await tool.invoke(_call(), None)
    assert r.ok
    assert r.content == "(empty)"


async def test_without_a_subject_it_fails_loudly():
    """没绑主体轨迹时必须明说 —— 这是装配错误，不是"轨迹恰好是空的"。"""
    r = await ReadTrajectoryTool().invoke(_call(), None)
    assert not r.ok
    assert r.error_type == "not_available"


# ---- 工具契约（与其它工具一致）----
def test_it_exposes_a_name_and_schema():
    tool = ReadTrajectoryTool(_subject())
    assert tool.name == "read_trajectory"
    schema = tool.schema()
    assert schema["name"] == tool.name
    assert schema["parameters"]["type"] == "object"
    assert tool.description


def test_the_schema_declares_both_slice_parameters():
    props = ReadTrajectoryTool().schema()["parameters"]["properties"]
    assert "start" in props and "end" in props


async def test_finish_reason_and_verdicts_are_visible_in_the_rendering():
    """Judge 要能看见模型说了什么，才谈得上审计。"""
    traj = (TB(run_id="j1").turn()
            .llm_response(text="VERDICT: pass")
            .run_end(status="ok").build())
    r = await ReadTrajectoryTool(traj).invoke(_call(), None)
    assert "llm.response" in r.content
    assert "VERDICT: pass" in r.content


# ---- 工具输出的内容必须可见（否则 judge 无法核对任何事）----
async def test_tool_result_content_is_rendered():
    """★ judge 的核心任务是核对"agent 说的话与工具实际返回的是否一致"。

    证据在 `tool.result.content` 里。初版只渲染工具名，于是 judge
    压根看不到证据 —— 那让 Agent-as-a-Judge 退化成"读一遍事件目录然后猜"。

    这条是实测踩出来的：抗注入探针永远找不到注射内容，
    因为注射语在被测轨迹的 tool.result 里，而 judge 看不见它。
    """
    tool = ReadTrajectoryTool(_subject())
    r = await tool.invoke(_call(), None)
    assert "code" in r.content, "工具返回的内容没有出现在渲染里"


async def test_llm_response_text_is_rendered():
    """Judge 要能看见模型说了什么，才谈得上发现"说的和做的不一致"。"""
    traj = (TB(run_id="s1").turn()
            .llm_response(text="All 12 tests passed")
            .tool_result(name="run_command", content="1 passed", ok=True)
            .run_end().build())
    r = await ReadTrajectoryTool(traj).invoke(_call(), None)
    assert "All 12 tests passed" in r.content


async def test_tool_call_arguments_are_rendered():
    tool = ReadTrajectoryTool(_subject())
    r = await tool.invoke(_call(), None)
    assert "a.py" in r.content


async def test_a_failed_tool_result_shows_its_error():
    traj = (TB(run_id="s1").turn()
            .llm_response(tool_calls=[("read_file", {"path": "ghost.py"})])
            .tool_result(name="read_file", content="", ok=False, error="not found",
                         error_type="not_found")
            .run_end().build())
    r = await ReadTrajectoryTool(traj).invoke(_call(), None)
    assert "FAIL" in r.content
    assert "not_found" in r.content


async def test_long_tool_output_is_truncated():
    """整条塞进去会把上下文撑爆，而 judge 通常只需要"有没有这句话"。"""
    traj = (TB(run_id="s1").turn()
            .llm_response(tool_calls=[("run_command", {"argv": ["cat", "big"]})])
            .tool_result(name="run_command", content="x" * 5000, ok=True)
            .run_end().build())
    r = await ReadTrajectoryTool(traj).invoke(_call(), None)
    assert len(r.content) < 1000


# ---- 推理必须渲染出来 ----
async def test_it_renders_the_models_reasoning():
    """★★ judge 判"为什么失败"时，模型的推理是信息量最大的那一份证据。

    没有这条渲染，`llm.response` 那一行只显示工具名或 `text` ——
    而实测里 `text` 经常是**空字符串**：17 条真轨迹里有些轮次
    `content` 为空、推理一千多字。那些轮次里模型在想什么，
    judge 一个字都看不到。

    实测症状（2026-09-16 全量真跑的某一条）：模型连续 5 轮在推理里写
    "no output? ... maybe the harness suppresses output"，而那些轮次
    的 `content` 全空、工具调用全是 `run_command`。
    judge 看到的是"重复调用同一个命令"，真因（它在排查环境）只存在于推理里。
    """
    subject = (TB(run_id="sut1").turn()
               .llm_response(reasoning="pytest returns exit 1 but no output?",
                             tool_calls=[("run_command", {"argv": ["pytest"]})])
               .tool_result(name="run_command", content="", ok=True)
               .run_end(status="ok").build())
    r = await ReadTrajectoryTool(subject).invoke(_call(), None)
    assert r.ok
    assert "pytest returns exit 1 but no output?" in r.content, (
        f"推理没有被渲染，judge 看不到模型自己的想法:\n{r.content}")


async def test_the_reasoning_excerpt_is_bounded():
    """★ 推理是可见输出的 10.8 倍，整段塞进 judge 的上下文会把证据淹掉。

    渲染要**有界**，理由与 `tool.result` 的截断相同：
    judge 需要的通常是"有没有这句话"，不是逐字全文。
    """
    subject = (TB(run_id="sut1").turn()
               .llm_response(reasoning="x" * 5000)
               .run_end(status="ok").build())
    r = await ReadTrajectoryTool(subject).invoke(_call(), None)
    assert r.ok
    assert len(r.content) < 1000, f"推理没有截断，渲染出 {len(r.content)} 字符"


async def test_a_response_with_only_reasoning_is_not_an_empty_line():
    """纯推理轮次（没有 text、没有工具调用）现在有内容可显示了。

    在那之前这一行渲染成 `llm.response`，什么都不带 ——
    与"这一轮什么都没发生"长得一模一样。
    """
    subject = (TB(run_id="sut1").turn()
               .llm_response(reasoning="I should check the reader module first")
               .run_end(status="ok").build())
    r = await ReadTrajectoryTool(subject).invoke(_call(), None)
    assert "I should check the reader module first" in r.content


async def test_responses_without_reasoning_render_exactly_as_before():
    """守卫：没有推理的响应（实测占 17%）渲染不能变。

    它们往往是真正干事的那几次（调工具的那一轮）——
    渲染形状变了会让 judge 已有的读法失效。
    """
    r = await ReadTrajectoryTool(_subject()).invoke(_call(), None)
    assert "tool_calls=['read_file']" in r.content
    assert "tool_calls=['finish']" in r.content
    assert "thinking" not in r.content
