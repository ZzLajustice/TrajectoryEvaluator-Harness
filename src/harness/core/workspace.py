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

# ---- 工作目录的落地布局 ----
#
# ## 为什么这些常量住在 `core/workspace.py` 而不是 `store/layout.py`
#
# `store/layout.py` 放的是 **runs/ 目录**的布局（快照名、索引名），
# 因为报告层要读它们却不许 import 组装层。而工作目录的布局有两个使用者：
# `core.workspace`（要在这里建目录）与 `orchestration.deps`（结果级评测要在
# 目录外部重新指向同一处）。两者都能看见 `core`，**但 core 看不见 store**
# （层级表：store 在 core 之上）—— 所以这里才是它该待的地方。
# 放错层的表现是 `lint-imports` 直接红，不会静默。
#
# 写成两处字面量的话，改一处漏一处时结果级评测会去一个**空目录**里跑
# 隐藏测试并拿到"全部通过" —— 那比报错危险得多。


def workspace_root(workdir: Path | str, *, case_id: str, run_id: str) -> Path:
    """SUT 工作目录的落地路径：`<workdir>/<case_id>/<run_id>`。"""
    return Path(workdir) / case_id / run_id


#: 隐藏验收测试在工作目录内的落地目录。
#: 以 `_` 开头是为了在 `ls` 里一眼看出它不是这个项目的一部分。
HIDDEN_DIR = "_hidden"

#: 隐藏验收测试在工作目录内的固定文件名。
HIDDEN_TEST_NAME = "test_hidden.py"

#: 隐藏测试在工作目录内的相对路径 —— 也是 `run_command` 的 argv 里用的那个。
HIDDEN_TEST_RELPATH = f"{HIDDEN_DIR}/{HIDDEN_TEST_NAME}"


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
        # 路径算法见本模块顶部的 workspace_root —— 结果级评测要在目录外指向同一处
        self.root = workspace_root(workdir, case_id=case_id, run_id=run_id)
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

        # overlay 在 patch **之前**：它是场景布置，补丁是针对场景的改动。
        # 顺序反过来的话，补丁会打在缺文件的树上 —— 而 `git apply` 的失败
        # 信息（"没有这个文件"）看起来像补丁本身坏了。
        if self.spec.overlay:
            await asyncio.to_thread(
                shutil.copytree, self.spec.overlay, self.root, dirs_exist_ok=True)

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
