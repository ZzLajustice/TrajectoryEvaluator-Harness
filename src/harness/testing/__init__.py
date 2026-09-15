"""测试工具 —— 作为产品的一部分发布。

用户写自己的评测器时同样需要构造事件序列，
「评测器可独立单测」这个卖点的兑现方式就是把这个能力交出去。
"""

from harness.testing.builder import TRUNCATION_MARKER, TrajectoryBuilder

__all__ = ["TRUNCATION_MARKER", "TrajectoryBuilder"]
