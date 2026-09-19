# Track B 用例的上游输入

`suites/codefix/cases/vendor_*` 两条用例**不是**从 `build_cases.py` 的字符串表
生成的 —— 它们的 bug 是**一个真实上游修复的反向**。这个目录放那些输入。

```
vendor_future_timestamp/
    upstream_fix.patch    上游 commit（= 生成的 fix.patch）
    test_hidden.py        隐藏验收测试（原样拷进用例目录）
vendor_date_signed_type/
    upstream_fix.patch
    test_hidden.py
```

生成器只做三件事：抄 `source` 目录 → 把 `upstream_fix.patch` **反向**应用得到
bug 态 → 取 `git diff` / `git diff -R` 作为 `bug.patch` / `fix.patch`。
所以补丁必然打得上去 —— 它就是这个 commit 本身，不是我拼出来的。

## 来源

| 用例 | 上游 | 修订 | 上游 issue |
|---|---|---|---|
| `vendor_future_timestamp` | [pallets/itsdangerous](https://github.com/pallets/itsdangerous) | `c30678d19e37011890e2374cca04f7789e101793` | [#126](https://github.com/pallets/itsdangerous/issues/126) |
| `vendor_date_signed_type` | 同上 | `526b1ea02934f529606dcbf3c3340ab429dcde04` | [#124](https://github.com/pallets/itsdangerous/issues/124) |

许可证 **BSD-3-Clause**，原文见各 vendored 树里的 `LICENSE.rst`。
信任链（PyPI sdist ≡ gitee 镜像 ≡ 本仓库）与 vendoring 适配见
`examples/vendor/itsdangerous-*/VENDOR.md`。

两条修订是**相邻的**：`c30678d` 是 `526b1ea` 的祖先。所以两棵树各自
"已含另一个修复"，与上游当时的状态一致 —— 而不是我挑了两块拼起来。

## 为什么不用上游自己的测试文件当隐藏测试

上游的回归测试住在 `tests/test_itsdangerous/test_timed.py`，它依赖
`conftest.py` 的 `signer` fixture、`FreezeMixin`、以及从兄弟模块 import 的
`TestSigner`。把它整份拖进来会把这些耦合一起拖进来，而且**它不是为
"在别人的工作目录里跑"写的**。

更重要的是：`bug.patch` 反向的是**整个 commit**，包括那个 commit 加的测试。
所以上游的回归测试在被测 agent 的树里**根本不存在** —— 直接把它拷回来
当隐藏测试是可行的，但那样子测的是"上游写的测试"，而这里想测的是
"这个 bug 真的被修掉了"。两者在 `test_future_age` 上恰好重合，在
`date_signed` 上不重合（见下）。

## 这两条隐藏测试各自的牙齿在哪

`vendor_date_signed_type` 的第二条测试构法很讲究，值得写下来：

    只坏时间戳段（签名仍有效）→ 走 `timestamp is None` 分支
                                 → **碰不到**被修的那一行，测不到过度修复
    签名失败 + 时间戳也解析不出 → 才走得到被修的那一行

**实测确认过**：拿前者去测"把 `timestamp` 无条件喂给 `timestamp_to_datetime()`"
这个过度修复，照样全绿。所以用的是后者（翻转签名最后一个字节，
即"令牌在传输里坏了一个字节"）。

这个区分不是洁癖 —— `_hidden/test_hidden.py` 里的注释记了同样的结论，
因为下一个人改这条测试时最自然的动作就是"简化成只坏时间戳段"。
