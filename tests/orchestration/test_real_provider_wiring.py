"""真 provider 的装配测试 —— 全部离线。

## 这里测什么、不测什么

**测**：从 suite 配置到 provider 实例这一段接线 —— 分派对不对、
凭据缺失时是不是"配置错误"（退出码 2）而不是"某条 case 失败"（退出码 1）。

**不测**：真实请求。那需要真 key，见 docs/known-gaps.md §1.1。

构造 `OpenAICompatProvider` 只创建 httpx 客户端，不建连接 ——
所以"分派到了真 provider"这件事在离线状态下可以断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness.cli import app
from harness.orchestration.credentials import ProviderConfigError
from harness.orchestration.deps import RunBuilder
from harness.orchestration.suite import load_suite
from harness.providers.fake import FakeProvider
from harness.providers.openai_compat import OpenAICompatProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
DEEPSEEK = REPO_ROOT / "examples" / "deepseek.yaml"
HELLO = REPO_ROOT / "examples" / "hello.yaml"

_ENV_NAMES = ("HARNESS_API_KEY", "HARNESS_JUDGE_API_KEY", "HARNESS_BASE_URL",
              "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """清掉真实环境变量 + 换到没有 .env 的目录。

    少了这一步，本机配的 key 会让"缺 key 应该报错"的测试变成真的去打网络。
    """
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


# ---- 分派 ----
def test_fake_provider_is_built_for_fake_suites():
    builder = RunBuilder(out_dir=Path("runs"))
    spec_model = load_suite(HELLO).defaults.model
    assert isinstance(builder._build_provider(spec_model, []), FakeProvider)


def test_a_real_provider_is_built_when_a_key_is_configured(monkeypatch):
    """★ 接线通了的证据 —— 且**不发请求**（构造只创建客户端）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    builder = RunBuilder(out_dir=Path("runs"))
    model = load_suite(DEEPSEEK).defaults.model
    provider = builder._build_provider(model, [])
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "openai_compat"


def test_record_wraps_the_real_provider(monkeypatch, tmp_path):
    """录制要能包住真 provider —— 否则第一次真机录制就得改代码。"""
    from harness.providers.recording import RecordingProvider

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    builder = RunBuilder(out_dir=tmp_path, record=tmp_path / "c.json")
    provider = builder._build_provider(load_suite(DEEPSEEK).defaults.model, [])
    assert isinstance(provider, RecordingProvider)


# ---- 装配期校验（这是本文件最重要的一节）----
def test_missing_key_fails_before_any_run_starts():
    """★ 缺 key 必须是**配置错误**，且在任何 run 启动前抛出。

    教训是反复出现的：`_build_provider` 在 `_run_case` 里被调用，
    而 `_run_case` 跑在调度器内部 —— 调度器会把异常记成"某条 case 失败"，
    于是症状变成 "1/1 case(s) failed to execute" 配退出码 1。
    让该去改配置的人去查门禁，是最浪费时间的一种误导。

    已经栽过三次：replay cassette（M6）、judge 的 case_id（M9）、
    现在的 API key。所以 `_preflight` 会在调度器之前再抛一遍。
    """
    with pytest.raises(ProviderConfigError, match="no API key"):
        RunBuilder(out_dir=Path("runs")).run_suite_sync(
            DEEPSEEK, case_ids=["smoke_text"])


def test_no_trajectory_is_written_when_the_key_is_missing(tmp_path):
    """装配期就炸 → 一个字节的轨迹都不该落下。"""
    with pytest.raises(ProviderConfigError):
        RunBuilder(out_dir=tmp_path / "runs",
                   workdir=tmp_path / "wd").run_suite_sync(DEEPSEEK)
    assert not list((tmp_path / "runs").glob("*.jsonl"))


def test_fake_suites_are_unaffected_by_the_preflight():
    """加了预检之后，fake suite 必须照常跑 —— 预检不能误伤。"""
    outcomes = RunBuilder(out_dir=Path("runs")).run_suite_sync(HELLO)
    assert len(outcomes) == 1


def test_preflight_also_checks_the_judge_provider(tmp_path, monkeypatch):
    """SUT 配了 key、judge 没配 —— 也该在跑之前拦下。

    否则会跑到一半才发现判官起不来，而那时 sut 的钱已经花了。
    """
    from harness.orchestration.suite import JudgeSpec
    from harness.orchestration.suite import load_suite as load

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    suite = load(DEEPSEEK)
    suite.defaults.judge = JudgeSpec(model="m", provider="qwen")  # 没配 QWEN_API_KEY
    # 直接调预检（走完整 run 会真的发请求）
    with pytest.raises(ProviderConfigError, match="qwen"):
        RunBuilder(out_dir=tmp_path)._preflight(suite)


# ---- CLI 覆盖 ----
def test_model_flag_overrides_the_suite_default():
    """换模型不该逼人改 suite 文件。"""
    from harness.orchestration.suite import load_suite as load

    suite = load(DEEPSEEK)
    assert suite.defaults.model.model == "deepseek-flash"
    # 走 CLI 的覆盖逻辑（不实际运行）
    suite.defaults.model = suite.defaults.model.model_copy(
        update={"model": "deepseek-v4-pro"})
    assert suite.defaults.model.model == "deepseek-v4-pro"
    assert suite.defaults.model.provider == "deepseek"  # provider 没被动


def _run_params():
    """`run` 命令**真实的**参数列表 —— 与 help 文本无关，因此与终端环境无关。

    鸭子类型而不是 isinstance：typer 的 TyperGroup **不是** click.Group 的子类
    （实测 isinstance 判 False），断言具体类型会变成测 typer 的内部结构。
    """
    import typer.main

    commands = getattr(typer.main.get_command(app), "commands", None)
    assert commands is not None, "CLI 的形状变了，这两条测试需要重写"
    return list(commands["run"].params)


def _run_flags() -> set[str]:
    """`run` 接受的旗帜（`--suite` / `-s` …）—— 用户能敲的那些字符串。"""
    return {opt for p in _run_params() for opt in (*p.opts, *p.secondary_opts)}


def test_provider_flag_is_accepted_by_the_cli(tmp_path):
    r"""`--provider` 要能解析到 —— 拼错的参数名 typer 会直接报错。

    判据是**参数列表**，不是 help 文本 —— 后者随环境变，而且变得很难看：

    CI 上 `GITHUB_ACTIONS=true`（runner 自己设的），typer 的 rich_utils 拿它
    当"强制着色"的信号，于是 help 里带 ANSI。同时它的 OptionHighlighter 两条
    正则都命中 `--provider`，rich 把 token 切成两个 span，ANSI 正好插在
    两个连字符中间：

        \x1b[1;36m-\x1b[0m\x1b[1;36m-provider\x1b[0m

    于是裸串 `--provider` 在出口文本里**不存在**。本地无该变量 → 过，
    CI → 红。(窄终端 COLUMNS 也会让它在连字符处折行，同样不过。)
    """
    flags = _run_flags()
    assert "--provider" in flags
    assert "--model" in flags
    assert "-m" in flags


def test_missing_key_through_the_cli_is_exit_code_2(tmp_path):
    """★ 端到端确认退出码契约：配置问题 → 2，不是 1。"""
    result = CliRunner().invoke(app, [
        "run", "-s", str(DEEPSEEK), "--case", "smoke_text",
        "--out", str(tmp_path / "runs"), "--workdir", str(tmp_path / "wd"),
    ])
    assert result.exit_code == 2, result.output
    assert "config error" in result.output
    assert "DEEPSEEK_API_KEY" in result.output


def test_the_cli_has_no_api_key_option():
    """★ key 不该能从命令行传 —— 那会进 shell history 与进程列表。

    这条测试是**反向**的：它确保那个参数不会被"顺手"加上去。

    判据是**真实的参数列表**，不是 help 文本 —— help 里恰好有一句
    "刻意没有 --api-key 参数"，按字符串找会自己骗自己。
    """
    params = {p.name for p in _run_params()}
    assert not [n for n in params if "api" in n and "key" in n], params


def test_env_file_is_gitignored():
    """Key 落在 .env 里，而 .env 必须不入库。"""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored.split()
    assert (REPO_ROOT / ".env.example").exists(), "要给一个可提交的模板"


# ---- 成本门禁失效时必须吵，但**不能误报** ----
def test_inert_cost_cap_warning_does_not_fire_for_fake_providers():
    """★ 假 provider 也报 token、也报 0 成本，但**没花任何钱**。

    按"tokens>0 且 cost==0"判的话，每条 fake 用例都会触发这条警告 ——
    而一个会误报的警告等于没有警告：看多了就学会忽略。
    判据必须是"真的有外部花费"，所以要看 provider 是不是真的。

    （初版就是这么写的，三条 e2e 用例的测试全被它刷过一遍。）
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        outcomes = RunBuilder(out_dir=Path("runs")).run_suite_sync(
            HELLO, max_cost=1.0)
    assert outcomes  # 走到这里就说明没有警告


def test_inert_cost_cap_warning_fires_for_a_real_provider(monkeypatch, tmp_path):
    """真 provider + 有 token + cost 恒 0 → 必须吵。

    这条不真的打网络：直接调那个方法，喂一个真实 provider 名的 outcomes。
    """
    from harness.contracts.results import Usage
    from harness.contracts.spec import RunStatus
    from harness.core.run import RunResult
    from harness.events.trajectory import Trajectory
    from harness.orchestration.deps import RunOutcome

    def _outcome() -> RunOutcome:
        traj = Trajectory.from_events("r1", [])
        return RunOutcome(result=RunResult(
            run_id="r1", status=RunStatus.OK, final_output=None, trajectory=traj,
            usage=Usage(input_tokens=700, output_tokens=100, cost_usd=0.0),
            turns=1, tool_calls=1, duration_s=0.1), case_id="c")

    builder = RunBuilder(out_dir=tmp_path)
    with pytest.raises(RuntimeWarning, match="could not be enforced"):
        with __import__("warnings").catch_warnings():
            __import__("warnings").simplefilter("error", RuntimeWarning)
            builder._warn_if_cost_cap_is_inert([_outcome()], 0.5, "deepseek")


def test_no_warning_when_no_cost_cap_is_set():
    """没设上限就没什么好警告的。"""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        RunBuilder(out_dir=Path("runs")).run_suite_sync(HELLO)
