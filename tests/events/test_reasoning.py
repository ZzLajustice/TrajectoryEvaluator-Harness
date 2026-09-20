"""推理内容的投影：从 provider 原始响应里取出模型的推理轨迹。

## 为什么这件事需要一个投影层，而不是给事件加字段

`LLMResponseEvent` 刻意没有 `reasoning` 字段 —— 项目规约是
「加字段前先自问能否从已有事件派生」，而推理**能**从 `raw` 派生
（`raw` 是保留原始响应的逃生舱，存在的理由就是 replay 无损）。

但"能派生"不等于"谁都能派生"：`raw.choices[0].message.reasoning_content`
是**厂商形状**。让它散落在评测器里，换个厂商就要改 N 处。

所以它住在 `events/reasoning.py`，与 `events/otel.py` 同一个角色 ——
L0 里唯一允许认识外部 payload 形状的地方，认识多少写多少，只有一份。

## 实测背景（2026-09-16 全量真跑，17 条轨迹 / 194 个响应）

| 事实 | 值 |
|---|---|
| 带推理的响应 | 161 / 194（83%） |
| 推理字符 / 可见输出字符 | 79,306 / 7,336 = **10.8x** |
| provider 直接上报 `reasoning_tokens` 的响应 | **194 / 194** |
| reasoning tokens / completion tokens | 20,089 / 58,149 = **34.5%** |

第 3 行是这套设计成立的前提：推理体量**不用数中文字符**，
provider 自己报。字符数只用来取内容，不用来做度量。
"""

from __future__ import annotations

from harness.events.reasoning import Reasoning, reasoning_of
from harness.events.trajectory import Trajectory
from harness.events.types import LLMResponseEvent
from harness.testing.builder import TrajectoryBuilder as TB


def _traj(*responses) -> Trajectory:
    tb = TB(run_id="r1", task="t")
    for kwargs in responses:
        tb = tb.turn().llm_response(**kwargs)
    return tb.run_end(status="ok").build()


# ---- 投影本身 ----
def test_reasoning_is_read_from_the_provider_payload():
    event = LLMResponseEvent(
        run_id="r", seq=3, model="m",
        raw={"choices": [{"message": {"reasoning_content": "let me look at the reader"}}]},
    )
    got = reasoning_of(event)
    assert got is not None
    assert got.text == "let me look at the reader"
    assert got.seq == 3


def test_a_payload_without_reasoning_yields_nothing():
    """绝大多数 provider 不带这个字段，不能因此炸，也不能造一个空条目。"""
    event = LLMResponseEvent(run_id="r", seq=1, model="m",
                             raw={"choices": [{"message": {"content": "hi"}}]})
    assert reasoning_of(event) is None


def test_a_completely_foreign_payload_yields_nothing():
    """`raw` 是逃生舱，形状不保证是 OpenAI 那一套（回放旧 cassette 时尤其）。"""
    for raw in ({}, {"choices": []}, {"choices": [{}]}, {"choices": "not-a-list"},
                {"choices": [{"message": None}]}, {"choices": [{"message": {}}]}):
        event = LLMResponseEvent(run_id="r", seq=1, model="m", raw=raw)
        assert reasoning_of(event) is None, raw


def test_whitespace_only_reasoning_is_treated_as_absent():
    """空推理与没有推理是两回事，但对下游都一样：没有内容可看。

    单独钉住是因为 `if reasoning:` 与 `if reasoning.strip():` 在测试里
    长得几乎一样 —— 而前者会让报告把"有 0 字符推理的响应"也算作带推理。
    """
    event = LLMResponseEvent(
        run_id="r", seq=1, model="m",
        raw={"choices": [{"message": {"reasoning_content": "   \n  "}}]},
    )
    assert reasoning_of(event) is None


def test_reasoning_tokens_are_read_from_usage():
    """体量取自 provider 上报的 `reasoning_tokens`，不是数中文字符。

    实测 194/194 个响应都带这个字段 —— 所以没有"回退到字符数"的必要，
    也就不该写那条回退路径（它永远不会被执行，却要一直维护）。
    """
    event = LLMResponseEvent(
        run_id="r", seq=1, model="m",
        raw={"choices": [{"message": {"reasoning_content": "thinking"}}],
             "usage": {"completion_tokens_details": {"reasoning_tokens": 137}}},
    )
    got = reasoning_of(event)
    assert got is not None and got.tokens == 137


def test_missing_reasoning_tokens_is_zero_not_a_crash():
    event = LLMResponseEvent(
        run_id="r", seq=1, model="m",
        raw={"choices": [{"message": {"reasoning_content": "thinking"}}]},
    )
    got = reasoning_of(event)
    assert got is not None and got.tokens == 0


def test_a_turn_with_no_reasoning_still_has_tool_calls():
    """守卫：加上推理抽取后，不带推理的普通响应必须原样可用。

    推理是**额外**的信息，不是替换 —— 一条轨迹里 17% 的响应没有它，
    而那些响应往往是真正干事的那几次（调工具的那一轮）。
    """
    traj = _traj({"text": "working", "tool_calls": [("read_file", {"path": "a.py"})]})
    assert traj.reasoning() == ()
    assert traj.tool_calls()  # 工具调用还是要在


# ---- Trajectory 上的入口 ----
def test_trajectory_reasoning_returns_one_entry_per_thinking_response():
    traj = _traj(
        {"reasoning": "explore first"},
        {"text": "no thinking here"},
        {"reasoning": "the bug is in stats.py", "reasoning_tokens": 42},
    )
    got = traj.reasoning()
    assert [r.text for r in got] == ["explore first", "the bug is in stats.py"]
    assert got[1].tokens == 42

    # 断言的是**挂对了响应**，不是 turn 的编号约定。
    # 写死 [0, 2] 会把测试绑在 `turn()` 从几开始上 —— 那是 builder 的实现细节，
    # 而这条测试要证明的是"推理属于带它的那一轮"。
    responses = traj.llm_responses()
    assert [r.seq for r in got] == [responses[0].seq, responses[2].seq]
    assert [r.turn for r in got] == [responses[0].turn, responses[2].turn]
    assert got[0].turn != got[1].turn  # 两条来自不同的轮次


def test_reasoning_entries_are_ordered_by_seq():
    """报告要按 seq 深链回事件，顺序错了就对不上。"""
    traj = _traj(*[{"reasoning": f"thought {i}"} for i in range(5)])
    seqs = [r.seq for r in traj.reasoning()]
    assert seqs == sorted(seqs)


def test_reasoning_on_an_empty_trajectory_is_empty_not_an_error():
    """退化输入绝不抛 —— 这是所有评测器的共同规约。"""
    traj = TB(run_id="r", task="t").run_end(status="ok").build()
    assert traj.reasoning() == ()


def test_the_builder_can_produce_reasoning():
    """★ `TrajectoryBuilder` 是**对外发布的产品能力**。

    用户写自己的评测器时，如果造不出带推理的轨迹，
    那"推理可被评测"这件事对他们就不成立 —— 只有我们的测试能验。
    """
    traj = _traj({"reasoning": "hello", "reasoning_tokens": 7})
    (only,) = traj.reasoning()
    assert (only.text, only.tokens) == ("hello", 7)
    assert isinstance(only, Reasoning)
