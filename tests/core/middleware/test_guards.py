"""三个守卫中间件的测试。

## 为什么要三个而非一个

它们的**拒绝语义不同**，而 `FailureClassifier` 依赖这个区分：

    permission  这个工具**能不能调**（ToolPolicy 的 allow/deny）
    sandbox     这个**参数**能不能传（路径越狱）
    policy      这条**规则**是否命中（自定义声明式规则）

合成一个中间件会让 `denied_by` 丢失来源信息，「agent 越权」和
「agent 传错参数」变成同一类失败。

## 共同的硬约束

**短路时也必须返回完整的 `ToolResult`**（call_id / name 齐全）——
否则评测器会看到「有 TOOL_CALL 无 TOOL_RESULT」的悬空配对。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.contracts.spec import MiddlewareSpec, ModelRef, RunRole, RunSpec, ToolPolicy
from harness.core.middleware.permission import PermissionMiddleware
from harness.core.middleware.policy import PolicyMiddleware
from harness.core.middleware.sandbox import SandboxMiddleware


def _spec(allow: list[str] | None = None, deny: list[str] | None = None) -> RunSpec:
    return RunSpec(
        role=RunRole.SUT, system_prompt="s",
        model=ModelRef(provider="fake", model="m"),
        tools=ToolPolicy(allow=allow, deny=deny or []),
    )


def _ctx(call: ToolCall, spec: RunSpec | None = None, root: str = "/tmp/ws"):
    return SimpleNamespace(call=call, spec=spec or _spec(), scratch={},
                           ws=SimpleNamespace(root=root))


async def _downstream(ctx):
    return ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=True, content="ran")


# ---- Permission ----
async def test_permission_denies_tool_outside_allow_list():
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "write_file", {}), _spec(allow=["read_file"])),
                        _downstream)
    assert r.ok is False
    assert r.denied_by == "permission"
    assert r.error_type == "permission_denied"


async def test_permission_allows_tool_in_allow_list():
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "read_file", {}), _spec(allow=["read_file"])),
                        _downstream)
    assert r.ok is True


async def test_permission_denies_explicit_deny_list():
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "run_command", {}), _spec(deny=["run_command"])),
                        _downstream)
    assert r.ok is False
    assert r.denied_by == "permission"


async def test_permission_allows_everything_when_allow_is_none():
    """allow=None 是默认值，表示全部允许 —— 不能被误实现成"一个都不给"。"""
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "anything", {})), _downstream)
    assert r.ok is True


async def test_permission_deny_wins_over_allow():
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    spec = _spec(allow=["read_file", "write_file"], deny=["write_file"])
    r = await mw.handle(_ctx(ToolCall("c1", "write_file", {}), spec), _downstream)
    assert r.ok is False


# ---- Policy ----
async def test_policy_denies_when_a_rule_matches():
    mw = PolicyMiddleware(MiddlewareSpec(
        name="policy",
        config={"rules": [{"tool": "run_command", "deny_if_arg_contains": "pip install"}]}))
    r = await mw.handle(
        _ctx(ToolCall("c1", "run_command", {"argv": ["pip", "install", "x"]})), _downstream)
    assert r.ok is False
    assert r.denied_by == "policy"


async def test_policy_passes_when_no_rule_matches():
    mw = PolicyMiddleware(MiddlewareSpec(
        name="policy",
        config={"rules": [{"tool": "run_command", "deny_if_arg_contains": "pip install"}]}))
    r = await mw.handle(_ctx(ToolCall("c1", "read_file", {"path": "a.py"})), _downstream)
    assert r.ok is True


async def test_policy_supports_regex_rules():
    mw = PolicyMiddleware(MiddlewareSpec(
        name="policy", config={"rules": [{"deny_if_arg_matches": r"rm\s+-rf"}]}))
    r = await mw.handle(
        _ctx(ToolCall("c1", "run_command", {"argv": ["rm", "-rf", "/"]})), _downstream)
    assert r.ok is False


async def test_policy_with_no_rules_passes_everything():
    mw = PolicyMiddleware(MiddlewareSpec(name="policy", config={}))
    r = await mw.handle(_ctx(ToolCall("c1", "whatever", {})), _downstream)
    assert r.ok is True


async def test_policy_tool_wildcard_matches_any_tool():
    mw = PolicyMiddleware(MiddlewareSpec(
        name="policy", config={"rules": [{"tool": "*", "deny_if_arg_contains": "SECRET"}]}))
    r = await mw.handle(
        _ctx(ToolCall("c1", "read_file", {"path": "SECRET.txt"})), _downstream)
    assert r.ok is False


# ---- Sandbox ----
async def test_sandbox_blocks_relative_path_escape(tmp_path):
    mw = SandboxMiddleware(MiddlewareSpec(name="sandbox"))
    r = await mw.handle(
        _ctx(ToolCall("c1", "read_file", {"path": "../../etc/passwd"}), root=str(tmp_path)),
        _downstream)
    assert r.ok is False
    assert r.denied_by == "sandbox"
    assert r.error_type == "path_escape"


async def test_sandbox_blocks_absolute_path_outside_workspace(tmp_path):
    mw = SandboxMiddleware(MiddlewareSpec(name="sandbox"))
    r = await mw.handle(
        _ctx(ToolCall("c1", "read_file", {"path": "C:/Windows/win.ini"}), root=str(tmp_path)),
        _downstream)
    assert r.ok is False


async def test_sandbox_blocks_dotdot_after_a_valid_prefix(tmp_path):
    """`sub/../../x` 的前缀合法 —— 只有 resolve 之后才知道它跑出去了。"""
    mw = SandboxMiddleware(MiddlewareSpec(name="sandbox"))
    r = await mw.handle(
        _ctx(ToolCall("c1", "write_file", {"path": "sub/../../evil.txt"}), root=str(tmp_path)),
        _downstream)
    assert r.ok is False
    assert r.error_type == "path_escape"


async def test_sandbox_passes_a_normal_path(tmp_path):
    mw = SandboxMiddleware(MiddlewareSpec(name="sandbox"))
    r = await mw.handle(
        _ctx(ToolCall("c1", "read_file", {"path": "src/a.py"}), root=str(tmp_path)),
        _downstream)
    assert r.ok is True


async def test_sandbox_ignores_tools_without_path_arguments(tmp_path):
    mw = SandboxMiddleware(MiddlewareSpec(name="sandbox"))
    r = await mw.handle(
        _ctx(ToolCall("c1", "finish", {"summary": "done"}), root=str(tmp_path)), _downstream)
    assert r.ok is True


# ---- 共同契约 ----
# 每个中间件都要**真的拒掉**下面这条输入，否则断言的是"没拒绝时的行为"。
# 三个中间件的拒绝条件不同，所以配置也不同：
#   permission  allow=[]  一个工具都不给
#   policy      规则命中 参数里含 "escape"
#   sandbox     路径越狱 "../escape"
_DENYING_MIDDLEWARE_FACTORIES = [
    pytest.param(
        lambda: PermissionMiddleware(MiddlewareSpec(name="permission")),
        id="permission"),
    pytest.param(
        lambda: PolicyMiddleware(MiddlewareSpec(
            name="policy", config={"rules": [{"deny_if_arg_contains": "escape"}]})),
        id="policy"),
    pytest.param(
        lambda: SandboxMiddleware(MiddlewareSpec(name="sandbox")),
        id="sandbox"),
]


@pytest.mark.parametrize("mw_factory", _DENYING_MIDDLEWARE_FACTORIES)
async def test_denial_always_returns_a_complete_result(mw_factory, tmp_path):
    """悬空配对是评测器的隐形杀手 —— 必须断言不是 None 且字段齐全。"""
    mw = mw_factory()
    r = await mw.handle(
        _ctx(ToolCall("c1", "x", {"path": "../escape"}), _spec(allow=[]), root=str(tmp_path)),
        _downstream)
    assert isinstance(r, ToolResult)
    assert r.call_id == "c1"
    assert r.name == "x"
    assert r.ok is False
    assert r.denied_by is not None


@pytest.mark.parametrize("mw_factory", _DENYING_MIDDLEWARE_FACTORIES)
async def test_denied_middleware_does_not_call_downstream(mw_factory, tmp_path):
    mw = mw_factory()
    called = False

    async def spy(ctx):
        nonlocal called
        called = True
        return ToolResult(call_id="c1", name="x", ok=True)

    await mw.handle(
        _ctx(ToolCall("c1", "x", {"path": "../escape"}), _spec(allow=[]), root=str(tmp_path)),
        spy)
    assert called is False
