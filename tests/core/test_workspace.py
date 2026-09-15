"""Workspace 生命周期测试。

核心设计：**工作目录放在项目内 `workdir/`，而非系统临时目录。**
理由见 `keep_on_failure` —— 失败的 case 要保留现场供调试，
而系统 tempdir 被清掉后现场就没了。
"""

from __future__ import annotations

from pathlib import Path

from harness.contracts.spec import WorkspaceSpec
from harness.core.executors.local import LocalExecutor
from harness.core.workspace import Workspace


def _ws(spec: WorkspaceSpec, tmp_path: Path, run_id: str = "r1") -> Workspace:
    return Workspace(spec, workdir=tmp_path / "workdir", run_id=run_id,
                     case_id="case1", executor=LocalExecutor())


async def test_copy_kind_copies_the_source_tree(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.py").write_text("x", encoding="utf-8")
    (src / "sub" / "b.py").write_text("y", encoding="utf-8")

    ws = _ws(WorkspaceSpec(kind="copy", source=str(src)), tmp_path)
    await ws.setup()
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "x"
    assert (ws.root / "sub" / "b.py").read_text(encoding="utf-8") == "y"


async def test_tempdir_kind_creates_an_empty_directory(tmp_path):
    ws = _ws(WorkspaceSpec(kind="tempdir"), tmp_path)
    await ws.setup()
    assert ws.root.is_dir()
    assert list(ws.root.iterdir()) == []


async def test_root_lives_under_the_project_workdir(tmp_path):
    """不是系统 tempdir —— 否则失败现场会被清掉。"""
    ws = _ws(WorkspaceSpec(kind="tempdir"), tmp_path)
    await ws.setup()
    assert (tmp_path / "workdir") in ws.root.parents


async def test_root_is_unique_per_run_id(tmp_path):
    a = _ws(WorkspaceSpec(kind="tempdir"), tmp_path, "r1")
    b = _ws(WorkspaceSpec(kind="tempdir"), tmp_path, "r2")
    await a.setup()
    await b.setup()
    assert a.root != b.root


async def test_successful_run_is_cleaned_up(tmp_path):
    ws = _ws(WorkspaceSpec(kind="tempdir", keep=False, keep_on_failure=False), tmp_path)
    await ws.setup()
    await ws.teardown(failed=False)
    assert not ws.root.exists()


async def test_failed_run_keeps_the_workspace_for_debugging(tmp_path):
    """这条是"用项目内 workdir 而非系统 tempdir"的核心理由。"""
    ws = _ws(WorkspaceSpec(kind="tempdir", keep_on_failure=True), tmp_path)
    await ws.setup()
    await ws.teardown(failed=True)
    assert ws.root.exists()


async def test_keep_overrides_everything(tmp_path):
    ws = _ws(WorkspaceSpec(kind="tempdir", keep=True), tmp_path)
    await ws.setup()
    await ws.teardown(failed=False)
    assert ws.root.exists()


async def test_keep_on_failure_false_cleans_up_on_failure(tmp_path):
    ws = _ws(WorkspaceSpec(kind="tempdir", keep_on_failure=False), tmp_path)
    await ws.setup()
    await ws.teardown(failed=True)
    assert not ws.root.exists()


async def test_patch_is_applied_after_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("original\n", encoding="utf-8")
    patch = tmp_path / "bug.patch"
    patch.write_text(
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-original\n+buggy\n", encoding="utf-8")

    ws = _ws(WorkspaceSpec(kind="copy", source=str(src), patch=str(patch)), tmp_path)
    await ws.setup()
    assert "buggy" in (ws.root / "a.py").read_text(encoding="utf-8")


async def test_bad_patch_raises_with_a_useful_message(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("original\n", encoding="utf-8")
    patch = tmp_path / "bad.patch"
    patch.write_text("this is not a patch\n", encoding="utf-8")

    ws = _ws(WorkspaceSpec(kind="copy", source=str(src), patch=str(patch)), tmp_path)
    try:
        await ws.setup()
    except RuntimeError as exc:
        assert "patch" in str(exc).lower()
    else:
        raise AssertionError("a malformed patch must not be silently ignored")


async def test_setup_resets_a_stale_directory(tmp_path):
    """上一次 run 留下的同 id 目录必须被清掉，否则会污染本次结果。"""
    ws = _ws(WorkspaceSpec(kind="tempdir"), tmp_path)
    await ws.setup()
    (ws.root / "stale.txt").write_text("old", encoding="utf-8")
    await ws.setup()
    assert not (ws.root / "stale.txt").exists()
