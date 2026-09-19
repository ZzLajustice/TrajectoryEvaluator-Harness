"""让 `src/` 布局在**未安装**的情况下可导入。

上游是 `pip install -e .` 之后跑测试的；被 vendor 进来之后没有安装这一步，
所以这里把 `src/` 加进 `sys.path`。

★ 这是 **vendoring 适配**，不是上游代码（见 VENDOR.md）。
少了它，SUT 连一条测试都跑不起来 —— 而症状会在报告里写成"模型不会修 bug"。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
