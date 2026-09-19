"""隐藏验收测试 —— vendor_date_signed_type

`BadTimeSignature.date_signed` 在**签名校验失败**这条路径上必须是
`datetime`。上游把它填成了原始整数。

## 这些测试不给被测 agent 看

它们在 run 结束、评测开始前才被拷进工作目录（`_hidden/test_hidden.py`）。
被测仓库自带的 `tests/` 覆盖的是"改完之后别把已有的东西弄坏"，
这里覆盖的是"这个 bug 真的被修掉了" —— 两者刻意不重合。

## 为什么这条路径值得单独守

`date_signed` 唯一有用的场合就是**排障**：拿到一个验不过的令牌，
想知道它是什么时候签的。而调用方会照着文档当 `datetime` 用
（`strftime` / `isoformat`）。所以在**唯一用得上它的那条路径**上
类型不符，等于这个属性白给。

类型不符本身不会抛异常 —— 它会在调用方离得很远的地方炸，
而那时已经看不出根因在这里。
"""

import sys
from datetime import datetime
from pathlib import Path


def _src_root() -> Path:
    """往上找到含 `src/itsdangerous` 的那一层，返回 `src/`。

    vendored 仓库是 `src/` 布局且**不装包**（见 VENDOR.md），
    所以导入根是 `src/` 而不是仓库根 —— 与 toyrepo 的平铺布局不同。
    """
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "src" / "itsdangerous").is_dir():
            return candidate / "src"
    raise RuntimeError("itsdangerous not found above cwd")


sys.path.insert(0, str(_src_root()))

import pytest

from itsdangerous import Signer
from itsdangerous.exc import BadTimeSignature
from itsdangerous.timed import TimestampSigner

SECRET = "secret-key"


def test_a_tampered_token_still_reports_a_datetime():
    """★ 签名对不上时，`date_signed` 仍要是 `datetime`。

    改动载荷而不动时间戳段 → 签名校验失败、但时间戳**还在**。
    这正是那条会把整数填进 `date_signed` 的分支。
    """
    tampered = TimestampSigner(SECRET).sign("my string").replace(b"my", b"other", 1)

    with pytest.raises(BadTimeSignature) as exc_info:
        TimestampSigner(SECRET).unsign(tampered)

    assert isinstance(exc_info.value.date_signed, datetime), (
        f"date_signed is {type(exc_info.value.date_signed).__name__}, "
        f"not datetime")


def test_a_corrupted_token_reports_none_rather_than_crashing():
    """★ 反向：**签名失败且时间戳也解析不出来**时必须是 `None`，不是异常。

    守的是这类修复最容易翻的车：把 `timestamp` 无条件地喂给
    `timestamp_to_datetime()`。这条路径上 `timestamp` 是 `None`
    （签名失败 → 载荷里那段时间戳段本来就解不出整数），
    于是 `datetime.fromtimestamp(None)` 直接 `TypeError` ——
    修完反而多出一个崩溃点。

    ★ 构法必须同时满足两个条件，缺一不可：

        sig_error 不为 None       → 才走得到被修的那一行
        timestamp 解析不出来      → 才让它是 None

    只坏时间戳段（签名仍有效）**测不到这个守卫** —— 那条路走的是
    `timestamp is None` 的分支，与本次修复无关。**实测确认过**：
    用它去测"无条件转换"这个过度修复，照样全绿。

    所以这里签一个带 `.<坏时间戳>` 的载荷，再把签名的最后一个字节翻掉 ——
    也就是"令牌在传输里被改坏了一个字节"。
    """
    signed = Signer(SECRET).sign(b"value.____________")
    corrupted = signed[:-1] + bytes([signed[-1] ^ 1])

    with pytest.raises(BadTimeSignature) as exc_info:
        TimestampSigner(SECRET).unsign(corrupted)

    assert exc_info.value.date_signed is None
