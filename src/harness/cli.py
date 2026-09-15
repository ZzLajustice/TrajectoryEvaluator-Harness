"""harness CLI。

## 退出码约定（CI 门禁依赖，任务 34 会用到全部）

    0  通过
    1  门禁未达标
    2  配置或加载错误
    3  预算超限
    4  基线缺失

本文件在任务 11 只实现 `run` 与 `trace`；
`report` / `diff` / `ci` 分别在任务 33 / 34 加入。
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from harness.orchestration.deps import RunBuilder, SuiteConfigError

app = typer.Typer(no_args_is_help=True, add_completion=False)

EXIT_CONFIG_ERROR = 2


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
) -> None:
    """运行一个 suite 并打印每条 run 的摘要。

    `--record` 与 `--replay` 互斥：录制要打真实模型，回放则完全离线。
    """
    if record is not None and replay is not None:
        typer.echo("config error: --record and --replay are mutually exclusive", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR)

    try:
        builder = RunBuilder(out_dir=out, record=record, replay=replay)
        results = builder.run_suite_sync(suite)
    except (FileNotFoundError, SuiteConfigError, KeyError) as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(EXIT_CONFIG_ERROR) from exc

    for r in results:
        tokens = r.usage.input_tokens + r.usage.output_tokens
        typer.echo(
            f"{r.run_id}  {r.status.value:<18} "
            f"turns={r.turns:<3} calls={r.tool_calls:<3} tokens={tokens}"
        )


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
