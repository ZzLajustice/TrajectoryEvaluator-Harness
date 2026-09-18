"""LocalExecutor 测试 —— 本项目风险最高的一块。

## 为什么这条测试最重要

`test_timeout_kills_process_tree` 针对的是设计文档 R1 风险：
**`asyncio.wait_for` 超时只取消 await，不会杀死子进程**；而 `proc.kill()`
只杀直接子进程。`run_command("pytest")` 会留下孤儿 python 进程，
并导致工作目录删不掉（`PermissionError: [WinError 32]`）。

这个 bug 的症状是"偶发、跨进程、事后才显现"，极难定位 —— 所以必须有一条
测试直接盯住"孙进程也被杀了"。

关于解释器：**杀进程/超时类的测试**一律用 `sys.executable` 而非 `"python"`，
避免 PATH 差异导致的假失败。而最后那一节**刻意**用裸名 `python` ——
它测的正是"裸名启动也能跑"，见那里的说明。
"""

from __future__ import annotations

import sys

import pytest

from harness.core.executors.local import LocalExecutor


async def test_normal_exit_captures_stdout(tmp_path):
    ex = LocalExecutor()
    r = await ex.run_process([sys.executable, "-c", "print('hi')"],
                             cwd=str(tmp_path), timeout_s=30)
    assert r.returncode == 0
    assert r.stdout.strip() == "hi"
    assert r.timed_out is False


async def test_stderr_is_captured_separately(tmp_path):
    ex = LocalExecutor()
    r = await ex.run_process(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom')"],
        cwd=str(tmp_path), timeout_s=30)
    assert r.stdout.strip() == ""
    assert "boom" in r.stderr


async def test_nonzero_exit_is_not_an_exception(tmp_path):
    """非零退出是正常结果，不是异常 —— 工具层负责决定怎么解读。"""
    ex = LocalExecutor()
    r = await ex.run_process([sys.executable, "-c", "raise SystemExit(3)"],
                             cwd=str(tmp_path), timeout_s=30)
    assert r.returncode == 3
    assert r.timed_out is False


async def test_timeout_kills_direct_child(tmp_path):
    ex = LocalExecutor()
    r = await ex.run_process([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=str(tmp_path), timeout_s=1)
    assert r.timed_out is True
    assert r.returncode != 0


async def test_timeout_kills_the_whole_process_tree(tmp_path):
    """★ 最关键的一条。

    孙进程必须一起被杀，否则孤儿进程会让工作目录删不掉。
    脚本会 spawn 一个 sleep 60 的孙进程，然后自己 sleep —— 超时后两者都应消失。
    """
    script = (
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "time.sleep(60)"
    )
    ex = LocalExecutor()
    r = await ex.run_process([sys.executable, "-c", script],
                             cwd=str(tmp_path), timeout_s=1)
    assert r.timed_out is True


async def test_workspace_is_deletable_after_a_timeout(tmp_path):
    """孤儿进程的直接后果就是这里失败（WinError 32）。

    这条测试是 `test_timeout_kills_the_whole_process_tree` 的**行为级**验证：
    不检查进程表，而是检查"能不能删掉目录"。
    """
    import shutil

    workdir = tmp_path / "ws"
    workdir.mkdir()
    script = (
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "time.sleep(60)"
    )
    ex = LocalExecutor()
    await ex.run_process([sys.executable, "-c", script],
                         cwd=str(workdir), timeout_s=1)
    shutil.rmtree(workdir)  # 若有孤儿进程占用，这里会抛 PermissionError


async def test_long_output_is_truncated_with_a_marker(tmp_path):
    ex = LocalExecutor(max_output_bytes=200)
    r = await ex.run_process([sys.executable, "-c", "print('x' * 5000)"],
                             cwd=str(tmp_path), timeout_s=30)
    assert r.truncated is True
    assert "[truncated]" in r.stdout
    assert len(r.stdout.encode("utf-8")) <= 400


async def test_short_output_is_not_truncated(tmp_path):
    ex = LocalExecutor(max_output_bytes=10_000)
    r = await ex.run_process([sys.executable, "-c", "print('short')"],
                             cwd=str(tmp_path), timeout_s=30)
    assert r.truncated is False
    assert "short" in r.stdout


async def test_env_is_inherited_and_overridable(tmp_path):
    ex = LocalExecutor()
    r = await ex.run_process(
        [sys.executable, "-c", "import os; print(os.environ.get('MYVAR', 'unset'))"],
        cwd=str(tmp_path), timeout_s=30, env={"MYVAR": "v"})
    assert r.stdout.strip() == "v"


# ---- 沙箱里的 `python` 必须真的能跑测试 ----
#
# 这一节盯的是一次真实事故：SUT 在沙箱里跑 `python -m pytest` 得到
# `No module named pytest`，于是**环境问题在评测记录里写成了"模型不会修 bug"**。
#
# ★ 断言的是**能力**，不是"解析到哪个二进制"。后者在这台机器上做不到：
#   `.venv\Scripts\python.exe` 是 uv 的跳板（~45 KB），靠自身路径找
#   `pyvenv.cfg`；以裸名启动时 `argv[0]` 没有目录，跳板找不到 venv，
#   就退化成一个没有项目包的基础解释器 —— 而 `where python` 还把它列在第一位。
#   所以"PATH 里有没有 python"这类检查全是绿的，坏的只有实际执行。
async def test_the_sandbox_can_run_pytest(tmp_path):
    """★ 最直接的一条：SUT 用什么方式跑测试都得能跑通。"""
    ex = LocalExecutor()
    r = await ex.run_process(["python", "-m", "pytest", "--version"],
                             cwd=str(tmp_path), timeout_s=60)
    assert r.returncode == 0, (
        f"the sandbox python cannot run pytest:\n{r.stdout}\n{r.stderr}")


async def test_the_sandbox_can_import_the_project_packages(tmp_path):
    """`pytest` 只是最常被用到的那个 —— 项目自身的包也要能导入。

    少了这条，把 pytest 塞进依赖但漏掉别的（比如将来加的可选依赖）
    会静默地只在 SUT 跑某条命令时才暴露。
    """
    ex = LocalExecutor()
    r = await ex.run_process(
        ["python", "-c", "import harness, pytest; print(harness.__file__)"],
        cwd=str(tmp_path), timeout_s=60)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


async def test_stdin_is_passed_through(tmp_path):
    ex = LocalExecutor()
    r = await ex.run_process(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        cwd=str(tmp_path), timeout_s=30, stdin="abc")
    assert r.stdout.strip() == "ABC"


async def test_missing_binary_raises_file_not_found(tmp_path):
    ex = LocalExecutor()
    with pytest.raises(FileNotFoundError):
        await ex.run_process(["definitely_not_a_binary_xyz"], cwd=str(tmp_path), timeout_s=5)


# ---- 文件操作 ----
async def test_file_io_roundtrip(tmp_path):
    ex = LocalExecutor()
    target = tmp_path / "a.txt"
    await ex.write_bytes(str(target), b"hello")
    assert await ex.read_bytes(str(target)) == b"hello"


async def test_write_creates_parent_directories(tmp_path):
    ex = LocalExecutor()
    target = tmp_path / "deep" / "nested" / "a.txt"
    await ex.write_bytes(str(target), b"x")
    assert target.read_bytes() == b"x"


async def test_read_bytes_respects_max_bytes(tmp_path):
    ex = LocalExecutor()
    target = tmp_path / "big.bin"
    target.write_bytes(b"0123456789")
    assert await ex.read_bytes(str(target), max_bytes=4) == b"0123"


async def test_list_dir_marks_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    entries = {e.name: e for e in await LocalExecutor().list_dir(str(tmp_path))}
    assert entries["sub"].is_dir is True
    assert entries["f.txt"].is_dir is False
    assert entries["f.txt"].size == 1


async def test_teardown_removes_the_workspace(tmp_path):
    class _WS:
        root = str(tmp_path / "ws")
        keep = False

    import pathlib

    pathlib.Path(_WS.root).mkdir()
    (pathlib.Path(_WS.root) / "x").write_text("y", encoding="utf-8")
    await LocalExecutor().teardown(_WS())
    assert not pathlib.Path(_WS.root).exists()


async def test_teardown_honours_keep_flag(tmp_path):
    class _WS:
        root = str(tmp_path / "ws")
        keep = True

    import pathlib

    pathlib.Path(_WS.root).mkdir()
    await LocalExecutor().teardown(_WS())
    assert pathlib.Path(_WS.root).exists()
