"""harness CLI。

## 退出码约定（CI 门禁依赖）

    0  通过
    1  门禁未达标
    2  配置或加载错误
    3  预算超限
    4  基线缺失

这些码是 `ci` 命令的对外契约 —— CI 流水线靠它们决定"红还是绿"。
每一个都对应一类**不同的人需要采取的行动**，所以不能合并：
配置错误该去改配置，预算超限该去查为什么烧了这么多，
基线缺失该去生成基线，门禁未达标该去看 agent 出了什么问题。
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from harness.orchestration.aggregator import aggregate, to_case_outcomes
from harness.orchestration.deps import (
    CaseExecutionError,
    RunBuilder,
    SuiteConfigError,
    SuiteCostExceeded,
)
from harness.orchestration.diff import diff_runs, format_diff
from harness.orchestration.suite import load_suite
from harness.report import data as report_data
from harness.report.html import render_report
from harness.report.terminal import metric_footnote, print_terminal_report
from harness.store.layout import SNAPSHOT_NAME
from harness.store.snapshot import SnapshotError

app = typer.Typer(no_args_is_help=True, add_completion=False)

# CLI 里评测状态的显示符号 —— 与 EvalStatus 一一对应
_STATUS_MARK = {
    "pass": "PASS", "fail": "FAIL", "warn": "WARN",
    "skipped": "SKIP", "error": "ERROR",
}

EXIT_NOT_MET = 1
EXIT_CONFIG_ERROR = 2
EXIT_BUDGET = 3
EXIT_NO_BASELINE = 4

# 这些异常都表示"配置或环境不对，不是 agent 不行" —— 统一映射到退出码 2
_CONFIG_ERRORS = (FileNotFoundError, SuiteConfigError, SnapshotError,
                  KeyError, ValueError)


@app.callback()
def main() -> None:
    """Agent 过程级评测 harness。"""


@app.command()
def run(
    suite: Path = typer.Option(..., "--suite", "-s", help="suite 配置文件路径。"),
    out: Path = typer.Option(Path("runs"), "--out", help="轨迹输出目录。"),
    record: Path | None = typer.Option(
        None, "--record", help="把 provider 响应录制到指定文件。"),
    replay: Path | None = typer.Option(
        None, "--replay", help="从指定文件回放 provider 响应（不发起真实调用）。"),
    evaluate: bool = typer.Option(
        False, "--evaluate", help="运行 suite 声明的评测器并打印结果。"),
    concurrency: int | None = typer.Option(
        None, "--concurrency", "-c", help="并发度；缺省用 suite 的 defaults.concurrency。"),
    case: list[str] | None = typer.Option(
        None, "--case", help="只跑指定 case_id（可重复）。"),
    workdir: Path = typer.Option(
        Path("workdir"), "--workdir",
        help="SUT 沙箱根目录，实际路径为 <workdir>/<case_id>/<run_id>/。"),
    max_cost: float | None = typer.Option(
        None, "--max-cost",
        help="suite 级成本上限（美元）。超出后不再启动新 case。"),
    model: str | None = typer.Option(
        None, "--model", "-m", help="覆盖 suite 的模型名（如 deepseek-v4-flash）。"),
    provider: str | None = typer.Option(
        None, "--provider", help="覆盖 suite 的 provider（如 deepseek）。"),
) -> None:
    """运行一个 suite 并打印每条 run 的摘要。

    `--record` 与 `--replay` 互斥：录制要打真实模型，回放则完全离线。
    `--evaluate` 默认关闭：跑 agent 与评 agent 是两件事，
    接入 LLM judge 后评测会产生额外成本，不该在不知情时发生。

    **真模型的 key 只从环境变量或 `.env` 读**（`DEEPSEEK_API_KEY` /
    `HARNESS_API_KEY`），刻意没有 `--api-key` 参数 —— 命令行参数会进
    shell history 和进程列表，那不是 key 该待的地方。
    """
    if record is not None and replay is not None:
        typer.echo("config error: --record and --replay are mutually exclusive", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR)

    try:
        builder = RunBuilder(out_dir=out, record=record, replay=replay,
                             workdir=workdir)
        outcomes = builder.run_suite_sync(
            suite, evaluate=evaluate, concurrency=concurrency, case_ids=case,
            max_cost=max_cost, model=model, provider=provider,
        )
    except _CONFIG_ERRORS as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR) from exc
    except SuiteCostExceeded as exc:
        typer.echo(f"budget exceeded: {exc}", err=True)
        raise typer.Exit(EXIT_BUDGET) from exc
    except CaseExecutionError as exc:
        # 执行期崩溃（不是 agent 失败）—— 与"门禁未达标"同类，退出码 1
        typer.echo(f"execution error: {exc}", err=True)
        raise typer.Exit(EXIT_NOT_MET) from exc

    for outcome in outcomes:
        r = outcome.result
        tokens = r.usage.input_tokens + r.usage.output_tokens
        # case_id 必须打出来 —— 并发跑 5 条时，没有它就无法把某行和某条用例对上
        label = outcome.case_id or r.run_id
        if outcome.repeat_index:
            label = f"{label}#{outcome.repeat_index}"
        typer.echo(
            f"{label:<24} {r.status.value:<18} "
            f"turns={r.turns:<3} calls={r.tool_calls:<3} tokens={tokens}"
        )
        for ev in outcome.evals:
            mark = _STATUS_MARK.get(ev.status.value, ev.status.value)
            typer.echo(f"      {mark:<8} {ev.evaluator:<20} {ev.summary}")
            for finding in ev.findings:
                # code 是机器可读的稳定标识 —— 报告聚合与回归比较都靠它，
                # 终端里也必须看得见，否则只能靠人眼看 message 找规律
                typer.echo(
                    f"          - [{finding.severity.value}] "
                    f"{finding.code}: {finding.message}"
                )
        if any(ev.error for ev in outcome.evals):
            # ERROR 是评测器自己坏了 —— 必须显式提示，不能混在 FAIL 里
            for ev in outcome.evals:
                if ev.error:
                    typer.echo(f"      ! {ev.evaluator} crashed: {ev.error}", err=True)


@app.command()
def trace(
    run_id: str = typer.Option(..., "--run-id", help="要查看的 run id。"),
    out: Path = typer.Option(Path("runs"), "--out", help="轨迹所在目录。"),
) -> None:
    """打印一条轨迹的事件流。"""
    path = out / f"{run_id}.jsonl"
    if not path.exists():
        typer.echo(f"no trajectory for run {run_id!r} under {out}", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR)

    for event in _read_events(path):
        seq = event.get("seq", "?")
        etype = event.get("type", "?")
        typer.echo(f"{seq!s:>4}  {etype:<18}{_brief(event)}")


# ---- 报告 / 对比 / 门禁 ----
@app.command()
def report(
    runs: Path = typer.Option(Path("runs"), "--runs", help="run 产物目录。"),
    fmt: str = typer.Option("terminal", "--format", "-f", help="terminal 或 html。"),
    out: Path | None = typer.Option(None, "--out", help="html 报告的输出路径。"),
    snapshot: Path | None = typer.Option(
        None, "--snapshot", help="指定快照文件；缺省用 <runs>/latest.json。"),
) -> None:
    """从已有产物生成报告。不重跑任何东西。"""
    try:
        data = report_data.collect(runs, snapshot=snapshot)
    except _CONFIG_ERRORS as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR) from exc

    if fmt == "terminal":
        print_terminal_report(
            data["aggregate"], suite_name=data["suite_name"],
            failure_modes=data["failure_modes"], judge=data["judge"],
        )
        typer.echo("\n" + metric_footnote())
        return

    if fmt != "html":
        typer.echo(f"config error: unknown format {fmt!r}; use terminal or html",
                   err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR)

    written = render_report(data, out or (runs / "report.html"))
    typer.echo(f"wrote {written}")


@app.command()
def diff(
    baseline: Path = typer.Option(..., "--baseline", help="基线快照。"),
    current: Path = typer.Option(..., "--current", help="当前快照。"),
) -> None:
    """对比两次 run 的结果。"""
    try:
        d = diff_runs(baseline, current)
    except _CONFIG_ERRORS as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR) from exc

    typer.echo(format_diff(d))
    # 有回归就以非零退出 —— 否则这条命令只能靠人眼看输出，进不了流水线
    if d["regressions"]:
        raise typer.Exit(EXIT_NOT_MET)


@app.command()
def ci(
    suite: Path = typer.Option(..., "--suite", "-s", help="suite 配置文件路径。"),
    baseline: Path | None = typer.Option(None, "--baseline", help="基线快照。"),
    fail_under: float = typer.Option(0.8, "--fail-under", help="pass_rate 下限。"),
    max_regressions: int = typer.Option(0, "--max-regressions", help="允许的回归条数。"),
    max_cost: float = typer.Option(2.0, "--max-cost", help="suite 级成本上限（美元）。"),
    out: Path = typer.Option(Path("runs"), "--out", help="run 产物目录。"),
    concurrency: int | None = typer.Option(None, "--concurrency", "-c"),
    workdir: Path = typer.Option(Path("workdir"), "--workdir"),
) -> None:
    """CI 门禁。退出码：0 通过 / 1 未达标 / 2 配置错误 / 3 预算超限 / 4 基线缺失。"""
    # 基线先查：缺失是**配置问题**，该在烧掉一整轮 suite 的钱之前拦下
    if baseline is not None and not Path(baseline).exists():
        typer.echo(f"baseline missing: {baseline}", err=True)
        raise typer.Exit(EXIT_NO_BASELINE)

    try:
        outcomes = RunBuilder(out_dir=out, workdir=workdir).run_suite_sync(
            suite, evaluate=True, concurrency=concurrency, max_cost=max_cost,
        )
    except SuiteCostExceeded as exc:
        typer.echo(f"budget exceeded: {exc}", err=True)
        raise typer.Exit(EXIT_BUDGET) from exc
    except CaseExecutionError as exc:
        typer.echo(f"execution error: {exc}", err=True)
        raise typer.Exit(EXIT_NOT_MET) from exc
    except _CONFIG_ERRORS as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR) from exc

    agg = aggregate(to_case_outcomes(outcomes))
    print_terminal_report(agg, suite_name=load_suite(suite).name)

    if agg["total_cost_usd"] > max_cost:
        typer.echo(f"FAIL: cost {agg['total_cost_usd']:.4f} > {max_cost}", err=True)
        raise typer.Exit(EXIT_BUDGET)

    if agg["pass_rate"] < fail_under:
        typer.echo(
            f"FAIL: pass_rate {agg['pass_rate']:.2%} < {fail_under:.2%}", err=True)
        raise typer.Exit(EXIT_NOT_MET)

    if baseline is not None:
        d = diff_runs(baseline, out / SNAPSHOT_NAME)
        typer.echo(format_diff(d))
        if len(d["regressions"]) > max_regressions:
            typer.echo(
                f"FAIL: {len(d['regressions'])} regression(s): {d['regressions']}",
                err=True)
            raise typer.Exit(EXIT_NOT_MET)

    typer.echo("PASS")


def _read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _brief(event: dict) -> str:
    """每个事件一行可读摘要 —— 这是过程级评测最基础的"看得见"。"""
    etype = event.get("type", "")
    if etype == "tool.call":
        args = json.dumps(event.get("arguments", {}), ensure_ascii=False)
        return f" {event.get('name', '?')}({args[:60]})"
    if etype == "tool.result":
        mark = "ok" if event.get("ok") else "FAIL"
        content = (event.get("content") or event.get("error") or "")[:60]
        return f" {event.get('name', '?')} [{mark}] {content}"
    if etype == "llm.response":
        text = (event.get("text") or "").strip()
        calls = event.get("tool_calls") or []
        if calls:
            return f" tool_calls={[c.get('name') for c in calls]}"
        return f" {text[:60]}" if text else ""
    if etype == "run.end":
        return f" status={event.get('status')} turns={event.get('turns')}"
    if etype == "error":
        return f" {event.get('where')}: {event.get('message', '')[:60]}"
    return ""
