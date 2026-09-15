r"""单文件 HTML 报告。

## 三条关键约束

1. **完全自包含** —— ECharts 内联、不引用任何外部资源。
   报告要能直接当 CI artifact 传递或作邮件附件，打开时不该产生任何网络请求。

2. **两条注入路径都要堵**：Jinja2 的 `autoescape` 管 HTML 文本上下文，
   但**管不到 `<script>` 内部** —— 那里必须手工把 `</` 转成 `<\\/`。
   case_id 来自 suite 配置，是外部输入。

3. **指标分列** —— `pass_rate` / `pass@k` / `flaky_rate` / 过程分各自独立成卡，
   不做加权、不给总分。

## 关于"报告里不许出现 http://"这条断言

计划里写的是 `assert "http://" not in text and "https://" not in text`。
**这条断言在 vendor 了 ECharts 之后必然失败，而且它测错了东西**：
ECharts 里含 `http://www.w3.org/2000/svg` 这类 **XML 命名空间标识符** ——
它们是 SVG 规范要求的常量，浏览器永远不会去请求它们。
真正的约束是「不引用外部**资源**」，判据应该是
`src=` / `href=` / `@import` / `url(http` 这些会触发网络请求的写法。
有测试同时盯住这两件事。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

_HERE = Path(__file__).parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE.parent / "static"

ECHARTS_FILE = "echarts.min.js"


def _safe_json(data: Any) -> str:
    r"""嵌进 `<script>` 的 JSON。

    `autoescape` **管不到 `<script>` 内部**，所以这里必须手工处理。
    把 `<` `>` `&` 转成 JSON 的 `\\uXXXX` 转义（Django `json_script` 同款做法）：

    - `</script>` 不再能提前闭合脚本块
    - 脚本块里一个裸的 `<` 都不剩，整块内容彻底惰性

    只转义 `</` 也够用（`<img>` 在脚本里只是文本），但"够用"依赖读者
    记得这条规则；转干净之后不必再推。JSON 层是等价的 —— `\\u003c` 解析回来
    就是 `<`，数据没有变形。
    """
    blob = json.dumps(data, ensure_ascii=False)
    return blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _echarts_source() -> str:
    path = _STATIC / ECHARTS_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"vendored ECharts missing: {path}. "
            "报告必须自包含，不能退回 CDN 引用 —— 见 src/harness/static/README.md"
        )
    return path.read_text(encoding="utf-8")


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        # ★ 必须开启：suite 名 / case_id 都是外部输入
        autoescape=select_autoescape(["html", "j2", "html.j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_report(data: dict[str, Any], out_path: Path | str) -> Path:
    """渲染单文件 HTML 报告，返回写出的路径。"""
    agg = data.get("aggregate") or {}
    cases = data.get("cases") or []

    html = _env().get_template("report.html.j2").render(
        suite_name=data.get("suite_name") or "",
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        aggregate=agg,
        cases=cases,
        failure_modes=data.get("failure_modes") or {},
        flaky=data.get("flaky") or agg.get("flaky_cases") or [],
        judge=data.get("judge"),
        metric_definitions=METRIC_DEFINITIONS,
        echarts_source=_echarts_source(),
        chart_data=_safe_json({
            "failure_modes": data.get("failure_modes") or {},
            "cost_quality": data.get("cost_quality") or [],
            "judge": data.get("judge"),
            "cases": cases,
        }),
    )
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：Windows 上默认会把 \n 翻成 \r\n，报告体积白白变大
    path.write_text(html, encoding="utf-8", newline="\n")
    return path


# 指标口径写进报告脚注 —— 数字离开口径就是噪音
METRIC_DEFINITIONS = {
    "pass_rate": "所有 repeat 都通过的 case 占比",
    "pass@k": "至少一次通过的 case 占比（不是经典 pass@k 无偏估计量）",
    "flaky_rate": "通过率严格落在 (0,1) 之间的 case 占比",
    "golden_score": "轨迹匹配分（过程分），与 outcome 分列，不做加权",
}
