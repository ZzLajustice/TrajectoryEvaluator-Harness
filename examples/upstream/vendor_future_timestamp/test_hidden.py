"""隐藏验收测试 —— vendor_future_timestamp

时间戳落在**将来**的签名必须被判过期，而不是当作有效签名放行。

## 这些测试不给被测 agent 看

它们在 run 结束、评测开始前才被拷进工作目录（`_hidden/test_hidden.py`）。
被测仓库自带的 `tests/` 覆盖的是"改完之后别把已有的东西弄坏"，
这里覆盖的是"这个 bug 真的被修掉了" —— 两者刻意不重合。

## 为什么不用 `freezegun`

上游自己的回归测试用 `freeze_time` 冻结"现在"，靠真实时钟与冻结时钟的
**相对位置**造出"将来"。能work，但它把一个确定性的判据挂在
"真实时钟必须晚于 1971"这个隐含前提上。

这里改用 `TimestampSigner` 自己的扩展点：`get_timestamp()`
（docstring 明说"must return an integer"，就是留给子类覆写的），
返回一个走快一小时的时钟。这既确定，又**正好就是 issue #126 的真实场景** ——
服务器时钟被 NTP 往回校正之后，先前签发的令牌时间戳落在了"将来"。
"""

import sys
import time
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

from itsdangerous.exc import SignatureExpired
from itsdangerous.timed import TimestampSigner

SECRET = "secret-key"


class _FastClock(TimestampSigner):
    """时钟走快一小时的 signer —— NTP 往回校正后那台机器的处境。"""

    def get_timestamp(self):
        return int(time.time()) + 3600


def test_a_token_dated_in_the_future_is_rejected():
    """★ 签名的时间戳落在将来时必须判过期。

    只检查"签得太久"（`age > max_age`）是不够的：一个将来签的令牌
    在真实时间追上它之前，`age` 一直是负数，于是**永远不会过期**。
    """
    signed = _FastClock(SECRET).sign("value")

    with pytest.raises(SignatureExpired) as exc_info:
        TimestampSigner(SECRET).unsign(signed, max_age=10)

    # 错误里也要带上签发时间 —— 排障时"这个令牌什么时候签的"是第一个问题
    assert exc_info.value.date_signed is not None


def test_a_token_signed_just_now_is_still_accepted():
    """反向：不能为了拒绝将来就把正常令牌一起拒了。

    "把整个 max_age 判断写死"是这类修复最常见的翻车方式，
    而它的症状是**所有**令牌都过期 —— 比原 bug 更严重。
    """
    signed = TimestampSigner(SECRET).sign("value")

    assert TimestampSigner(SECRET).unsign(signed, max_age=3600) == b"value"
