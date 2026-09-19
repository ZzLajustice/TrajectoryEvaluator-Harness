# vendored: itsdangerous @ 526b1ea

| | |
|---|---|
| 来源 | https://github.com/pallets/itsdangerous |
| 修订 | `526b1ea02934f529606dcbf3c3340ab429dcde04` |
| 许可证 | **BSD-3-Clause**，原文见 `LICENSE.rst`（随源码一并保留） |
| 抓取日期 | 2026-09-18 |

## 为什么锁定单个修订，而不是取最新版

每条用例的 `bug.patch` 是**这个修订里那个真实修复的反向**，`fix.patch` 就是上游那次修复本身。

实测：把同一个 fix 反向应用到更新的树上时 hunk 上下文会对不上（后续提交改动了相邻行）——
四个候选 fix 里只有一个能在最新树上反向应用。所以用例固定它自己的上游修订，补丁必然打得上去。

## 可信链（怎么知道这不是被篡改过的镜像）

```
PyPI sdist (sha256 与 PyPI 索引核对一致)  ==  gitee 镜像 clone@2.2.0  ==  本目录
```

- PyPI 两个 sdist 的 sha256 与索引公布的哈希**逐一核对通过**
- gitee 镜像 clone@2.2.0 与 sdist 的文件**归一化换行后逐字节相同**
  （差异纯粹是 `core.autocrlf`：clone 在 Windows 上检出成 CRLF，sdist 是 LF）
- 本目录与该修订的上游文件（`CHANGES.rst` / `LICENSE.rst` / `src/` / `tests/`）
  **归一化后 18/18 逐字节相同** —— 比对的是 git blob，不是工作树的检出结果

## 相对上游只有两处改动，都是 vendoring 适配

1. 新增 `conftest.py`：上游是装成包跑的（`src/` 布局 + pip install），
   vendor 进来没有安装这一步，所以把 `src/` 加进 `sys.path`。
   **不加的话 SUT 连测试都跑不起来** —— 而那会变成"模型不会修 bug"。
2. 不收 `pyproject.toml` / `setup.py` / `docs/`：前两者的 pytest 与打包配置
   会和 harness 自己的冲突，后者与用例无关。

所有文件已统一为 **LF**：补丁是 LF（`git show` 的输出），
CRLF 的树配 LF 的补丁会打不上 —— 实测踩过，症状是 `git apply` 报
"patch does not apply" 并指着一个看起来完全正确的 hunk。

★ **重新 vendor 时注意**：从 clone 拷贝文件**必须显式归一化**，
因为 `core.autocrlf` 会把检出结果写成 CRLF —— 而 `git ls-tree` 里的 blob
是 LF。本次 `LICENSE.rst` 就是这么带上来的（其余文件当时已归一化），
它不参与打补丁所以没有功能性后果，但"树里混着两种换行"这件事本身就是坑。
`git add` 时那声 `CRLF will be replaced by LF` 的 warning 是唯一的提示。
