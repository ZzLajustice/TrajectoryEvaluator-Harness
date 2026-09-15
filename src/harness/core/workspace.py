"""工作目录生命周期。

## 为什么放在项目内 `workdir/` 而非系统临时目录

`keep_on_failure` 是核心理由：失败的 case 要保留现场供调试。
系统 tempdir 被清掉后现场就没了 —— 而**恰恰是失败的 run 最需要现场**。

## 目录形态

    <workdir>/<case_id>/<run_id>/

按 case 分层是为了人工翻看时能快速定位；按 run 分层是为了并发的
`--repeat N` 之间互不干扰。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from harness.contracts.spec import WorkspaceSpec


class Workspace:
    def __init__(
        self,
        spec: WorkspaceSpec,
        *,
        workdir: Path | str,
        run_id: str,
        executor: Any,
        case_id: str = "case",
    ) -> None:
        self.spec = spec
        self.executor = executor
        self.root = Path(workdir) / case_id / run_id
        self.keep = spec.keep
        self.keep_on_failure = spec.keep_on_failure

    async def setup(self) -> None:
        """准备干净的工作目录。

        **先清掉同 id 的旧目录** —— 上一次 run 的残留会污染本次结果，
        而"结果莫名变了"是最难排查的一类问题。
        """
        if self.root.exists():
            await asyncio.to_thread(shutil.rmtree, self.root)
        await asyncio.to_thread(self.root.parent.mkdir, parents=True, exist_ok=True)

        if self.spec.kind in {"copy", "git_worktree"} and self.spec.source:
            await asyncio.to_thread(shutil.copytree, self.spec.source, self.root)
        else:
            await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)

        if self.spec.patch:
            await self._apply_patch(Path(self.spec.patch))

        await self.executor.setup(self)

    async def _apply_patch(self, patch: Path) -> None:
        """用 `git apply` 打补丁。

        失败必须**抛异常而非静默忽略** —— 补丁没打上意味着这个 case
        测的不是我们以为的那个 bug，结果全部无意义。
        """
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--whitespace=nowarn", str(patch.resolve()),
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"failed to apply patch {patch}: {err.decode('utf-8', errors='replace')}"
            )

    async def teardown(self, *, failed: bool = False) -> None:
        should_keep = self.keep or (failed and self.keep_on_failure)
        if should_keep:
            return
        await self.executor.teardown(self)
