"""本地执行器：subprocess + 超时 + **进程树杀死**。

## Windows 关键约束（踩过坑，勿改）

1. **`asyncio.create_subprocess_exec` 在 Windows 上只支持 `ProactorEventLoop`**
   （Python 3.8+ 的默认）。绝不要设置 `WindowsSelectorEventLoopPolicy`，
   否则直接 `NotImplementedError`。`uvloop` 在 Windows 不可用，不要依赖它。

2. **`asyncio.wait_for` 超时只取消 await，不杀子进程。**
   更糟的是 `proc.kill()` 只杀**直接**子进程 —— `run_command("pytest")`
   会留下孤儿 python 进程，并导致工作目录删不掉
   （`PermissionError: [WinError 32]`）。

   症状是"偶发、跨进程、事后才显现"，极难定位。正确做法是
   `CREATE_NEW_PROCESS_GROUP` 启动 + 超时后 `taskkill /F /T /PID`（`/T` 杀整棵树），
   杀完 `await proc.wait()` 收尸。

3. **顺序不能反**：杀进程树是目录清理重试能生效的前提。
   先重试清理再杀进程，重试多少次都没用。

4. `preexec_fn` 在 Windows 上不被支持，必须只在 POSIX 分支传。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from harness.contracts.protocols import DirEntry, ProcessResult
from harness.core.workspace import force_rmtree

_TRUNCATION_MARKER = "\n... [truncated] ...\n"


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


class LocalExecutor:
    """在本机执行命令与文件操作。

    **文件操作也走这里**（而不是工具直接 `Path.read_text`）——
    这样将来换 `DockerExecutor` 时文件工具不用改一行。
    """

    name = "local"

    def __init__(self, max_output_bytes: int = 200_000) -> None:
        self.max_output_bytes = max_output_bytes

    # ---- 生命周期 ----
    async def setup(self, ws: Any) -> None:
        Path(ws.root).mkdir(parents=True, exist_ok=True)

    async def teardown(self, ws: Any) -> None:
        """清理工作目录。对 `PermissionError` 重试。

        重试能生效的前提是**孤儿进程已被杀掉**（见模块 docstring 第 3 条）。
        """
        if getattr(ws, "keep", False):
            return
        root = Path(ws.root)
        if not root.exists():
            return
        for attempt in range(3):
            try:
                # 用 force_rmtree 而不是 shutil.rmtree：Windows 上 git 的
                # 对象文件是只读的，直接 rmtree 会 PermissionError ——
                # 而重试解决不了只读位，必须显式清掉
                await asyncio.to_thread(force_rmtree, root)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05)

    # ---- 进程执行 ----
    def _child_env(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        r"""子进程的环境 —— 保证沙箱里的 `python` **真的能跑这个项目的测试**。

        ## 踩到的坑（实测，Windows + uv）

        `.venv\\Scripts\\python.exe` 只有 ~45 KB，它不是 Python，
        而是 uv 的**跳板**：靠自身路径向上找 `pyvenv.cfg` 来建立 venv 上下文。
        以裸名启动时 `argv[0]` 里没有目录，跳板找不到 venv，
        于是**退化成一个没有项目包的基础解释器**：

            $ python -c "import sys; print(sys.prefix)"
            C:\\Users\\...\\uv\\python\\cpython-3.12-...      ← 不是 .venv
            $ python -m pytest
            No module named pytest

        而 `where python` 明明把 `.venv\\Scripts\\python.exe` 列在第一位，
        绝对路径调用它也一切正常。**同一个文件，换个调用方式就换了个身份** ——
        所以"PATH 里有没有 python"这个检查通不过任何检验，
        真正会坏的是"解析到哪一个、带不带 venv 上下文"。

        ## 为什么用 PYTHONPATH 而不是继续调 PATH

        实测：把 PATH 清成只剩 `.venv\\Scripts`，裸名 `python` 仍然落到基础解释器。
        也就是说路径顺序根本不是决定因素 —— 跳板拿不到自身目录就没救。
        所以这里不再跟名字解析较劲，直接保证**能力**：
        把 venv 的 site-packages 放进 `PYTHONPATH`，
        于是沙箱里**任何**解释器都能 `import pytest` / `import csvlite`。

        ## 为什么这件事必须由 harness 负责

        它的反面是：SUT 拿到一个跑不了测试的环境 →
        报告里写"模型不会修 bug"。**被测的从来不该是环境考古能力。**
        它还顺带消掉一个更隐蔽的分歧：SUT 若在另一个 Python 上验证，
        就会出现"agent 看到全绿、评分看到红"，
        而那种分歧极难归因，会让整份评测数据不可信。
        """
        merged = {**os.environ, **(extra or {})}
        # 纯乐观项：在跳板行为正常的机器上，这能让 `python` 直接指向 venv
        interpreter_dir = str(Path(sys.executable).resolve().parent)
        merged["PATH"] = interpreter_dir + os.pathsep + merged.get("PATH", "")
        # 真正的保证：无论哪个解释器被启动，项目包都可导入
        site_packages = sysconfig.get_paths()["purelib"]
        merged["PYTHONPATH"] = site_packages + os.pathsep + merged.get("PYTHONPATH", "")
        return merged

    async def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> ProcessResult:
        is_windows = os.name == "nt"
        # getattr 而非直接引用：`os.setsid` 在 Windows 的 typeshed 里不存在，
        # 直接写会让类型检查在 Windows 上报错，也确实是运行时的 AttributeError。
        posix_setsid = getattr(os, "setsid", None) if not is_windows else None

        proc = await asyncio.create_subprocess_exec(
            *[str(a) for a in argv],
            cwd=cwd,
            env=self._child_env(env),
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if is_windows else 0,
            preexec_fn=posix_setsid,
        )

        timed_out = False
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None),
                timeout=timeout_s,
            )
        except (TimeoutError, asyncio.TimeoutError):
            timed_out = True
            await self._kill_tree(proc.pid)
            # 收尸并拿回已产出的部分输出 —— 超时的进程也可能已经打印了有用信息
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, ProcessLookupError):
                out, err = b"", b""
        finally:
            if proc.returncode is None:
                await self._kill_tree(proc.pid)

        stdout, truncated = self._cap(_decode(out or b""))
        stderr, _ = self._cap(_decode(err or b""))
        return ProcessResult(
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
            timed_out=timed_out,
            truncated=truncated,
        )

    async def _kill_tree(self, pid: int) -> None:
        """杀掉整棵进程树。

        Windows 上 `/T` 是关键 —— 没有它，`pytest` 派生的 python 子进程会成为孤儿。
        """
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                import signal

                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            # 进程可能已自行退出 —— 不是错误
            pass

    def _cap(self, text: str) -> tuple[str, bool]:
        raw = text.encode("utf-8")
        if len(raw) <= self.max_output_bytes:
            return text, False
        half = max(1, self.max_output_bytes // 2)
        capped = _decode(raw[:half]) + _TRUNCATION_MARKER + _decode(raw[-half:])
        return capped, True

    # ---- 文件操作 ----
    async def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        p = Path(path)
        if max_bytes is not None:
            with p.open("rb") as fh:
                return fh.read(max_bytes)
        return await asyncio.to_thread(p.read_bytes)

    async def write_bytes(self, path: str, data: bytes) -> None:
        p = Path(path)
        await asyncio.to_thread(p.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def list_dir(self, path: str) -> list[DirEntry]:
        def _scan() -> list[DirEntry]:
            out: list[DirEntry] = []
            for entry in sorted(Path(path).iterdir()):
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
                out.append(DirEntry(name=entry.name, is_dir=is_dir, size=size))
            return out

        return await asyncio.to_thread(_scan)


def _python() -> str:
    """当前解释器路径。测试用 —— 避免 PATH 里的 python 版本不确定。"""
    return sys.executable
