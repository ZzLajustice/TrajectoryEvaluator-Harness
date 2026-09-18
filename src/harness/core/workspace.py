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
import os
import shutil
import stat
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


def force_rmtree(path: Path | str) -> None:
    """删目录树，遇到只读文件**先去掉只读位**再删。

    ★ Windows 专属坑：git 把 `.git/objects/**` 建成**只读**，
    而 `shutil.rmtree` 碰到只读文件会抛 `PermissionError: [WinError 5]`。

    关键在于**重试救不了它** —— 只读位不会自己消失，必须显式清掉。
    而 `LocalExecutor.teardown` 里那套重试是为"文件正被占用"设计的
    （孤儿进程被杀之后占用会释放），两者成因不同、解法也不同。
    实测：工作目录一旦成为 git 仓库，下一次 setup 清理旧目录就炸。

    用在两处：`Workspace.setup` 清旧目录、`LocalExecutor.teardown` 清现场。
    """
    def _clear_readonly(func: Any, target: Any, _exc: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            # 清不掉就放弃这一项 —— 让上层的重试/报错去处理
            pass

    shutil.rmtree(path, onexc=_clear_readonly)


#: 隐藏验收测试在工作目录内的落地目录。
#: 以 `_` 开头是为了在 `ls` 里一眼看出它不是这个项目的一部分。
HIDDEN_DIR = "_hidden"

#: 隐藏验收测试在工作目录内的固定文件名。
HIDDEN_TEST_NAME = "test_hidden.py"

#: 隐藏测试在工作目录内的相对路径 —— 也是 `run_command` 的 argv 里用的那个。
HIDDEN_TEST_RELPATH = f"{HIDDEN_DIR}/{HIDDEN_TEST_NAME}"


def _patch_targets(patch: Path) -> list[str]:
    """补丁声明要写入的文件（相对工作目录）。

    取 `+++ b/<path>` 行 —— 那是"打完补丁后这个文件应当在"的权威声明。
    `/dev/null` 表示删除，跳过：删除之后文件本来就不该存在。

    只做最小解析，不引 patch 库：我们只需要路径，不需要理解 hunk。
    """
    targets: list[str] = []
    for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip()
        if raw == "/dev/null":
            continue
        # git diff 的写法是 `b/<path>`；少数工具不写前缀
        targets.append(raw[2:] if raw.startswith("b/") else raw)
    return targets


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
            await asyncio.to_thread(force_rmtree, self.root)
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

        # git 仓库**只为打补丁而建**（没有补丁时它没有任何用处，
        # 而 `tempdir` 类工作区本就该是空的 —— 有测试盯着这一点）。
        #
        # 顺序不能反：`git apply` 会向上找最近的仓库，而 `workdir/` 默认就在
        # harness 自己的仓库里 —— 那样它会把补丁路径解析到**外层仓库根**，
        # 找不到就 `Skipped patch` 然后返回 0（详见 `_apply_patch`）。
        if self.spec.patch:
            await self._ensure_repo()
            await self._apply_patch(Path(self.spec.patch))
            # ★ 把注入的 bug **提交**为基线，否则它会以"未提交的改动"形式
            # 出现在 `git diff` 里 —— 而那正好是一行答案：
            #     -    return sum(values) / len(values)
            #     +    return sum(values) / (len(values) - 1)
            # 任何先跑 `git diff` 的 agent 都能直接抄。实测模型确实会跑
            # `git diff HEAD` 和 `git log`，所以这不是假想的风险。
            await self._commit_baseline()

        await self.executor.setup(self)

    async def _git(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed in {self.root}: "
                f"{err.decode('utf-8', errors='replace')}")

    async def _ensure_repo(self) -> None:
        """让工作目录成为一个独立的 git 仓库。

        三个后果，都是我们想要的：

        1. **补丁不再被静默跳过** —— `git apply` 找到的是工作目录自己的
           `.git`，路径按工作目录解析，这才是它该有的语义。
        2. **agent 的 git 命令回答的是它自己的问题。** 实测真模型跑了
           `git log --oneline -20` 与 `git status`，而在没有独立仓库时
           它们回答的是**外层 harness 仓库**的历史 —— 完全是误导。
        3. 给 agent 一条"回到起点"的路（`git checkout -- .`），
           这是真实工程里最常见的求助动作之一。

        `git_worktree` 类型自带 `.git`，跳过初始化。
        """
        if (self.root / ".git").exists():
            return
        await self._git("init", "-q")

    async def _commit_baseline(self) -> None:
        """把当前状态（**含注入的 bug**）提交为基线。

        身份用 `-c` 显式给：CI 与容器里常常没有全局 git 身份，
        `git commit` 会直接失败 —— 而那个报错的措辞
        （"Please tell me who you are"）完全不会指向真正的原因。
        """
        await self._git("add", "-A")
        await self._git("-c", "user.email=harness@example.invalid",
                        "-c", "user.name=harness",
                        "commit", "-q", "--no-verify", "-m", "baseline")

    async def _apply_patch(self, patch: Path) -> None:
        """用 `git apply` 打补丁，然后**验证补丁真的落地了**。

        ## 为什么不能只信退出码

        实测（git 2.39，Windows）：**当工作目录位于某个 git 仓库内部时**，
        `git apply` 对"目标文件不存在"的补丁会打印

            Skipped patch 'csvlite/stats.py'.

        然后**返回 0**。也就是说补丁一行都没打上，而调用方看到的是"成功"。

        这正是那次事故的最后一环：`source` 忘了配 → 工作目录是空的 →
        `git apply` 静默跳过 → SUT 拿到一个空目录、一路对着空气干活 →
        报告上写的是"模型不会修 bug"。**四个门全绿**，
        因为整套测试都在仓库外的 `tmp_path` 里跑，而那里 `git apply`
        会老老实实报错。

        ## 也不能只检查"目标文件存在"

        **第二次踩的**（同一个静默跳过，更隐蔽的一层）：工作目录里
        `csvlite/stats.py` 明明存在（`source` 拷进来的），于是"文件在不在"
        这个判据全绿 —— 而 `git apply` 一行都没打上，因为它眼中的
        目标路径是**外层仓库根**下的 `csvlite/stats.py`，那里没有这个文件。

        后果比第一次更糟：工作区里是**修好的**代码，隐藏测试自然通过，
        于是 `OutcomeGrader` 报 PASS —— 一个假阳性。而真模型那 12 轮
        全在找"到底要我修什么"，报告上写的是"未完成"。

        所以判据只能是**内容确实变了**：打补丁前后把目标文件的字节比一遍。
        这不依赖 git 的措辞、版本、退出码，也不依赖当前目录落在哪。
        """
        targets = _patch_targets(patch)
        before = {t: (self.root / t).read_bytes() if (self.root / t).exists() else None
                  for t in targets}

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

        changed = []
        for target in targets:
            path = self.root / target
            after = path.read_bytes() if path.exists() else None
            if after != before[target]:
                changed.append(target)
            elif after is None:
                # `+++ /dev/null` 之外的删除：文件本来就该存在
                raise RuntimeError(
                    f"patch {patch} reported success but {target} is still absent")
        if not changed:
            raise RuntimeError(
                f"patch {patch} reported success but changed NOTHING in "
                f"{self.root}. This is the silent-skip case: when the working "
                f"directory sits inside a git repository, `git apply` resolves "
                f"paths against the OUTER repo and silently skips the patch, "
                f"exiting 0. Check that `workspace.source` pointed at something "
                f"and that the patch's paths match it.")

    async def teardown(self, *, failed: bool = False) -> None:
        should_keep = self.keep or (failed and self.keep_on_failure)
        if should_keep:
            return
        await self.executor.teardown(self)
