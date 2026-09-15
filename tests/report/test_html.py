"""HTML 报告测试。

## 本文件的核心约束

**报告必须完全自包含。** 它要能当 CI artifact 传递、作邮件附件，
打开时不产生任何网络请求。ECharts 因此是 vendor 进仓库、内联进 `<script>` 的。

## 关于"报告里不许出现 http://"

计划里断言 `"http://" not in text and "https://" not in text`。
**这条在 vendor 了 ECharts 之后必然失败，而且它测错了东西**：
ECharts 内含 `http://www.w3.org/2000/svg` 这类 **XML 命名空间标识符** ——
SVG 规范要求的常量，浏览器永远不会去请求。

真正要测的是「不引用外部**资源**」，判据是 `src=`/`href=`/`@import`/`url(http`
这类会触发网络请求的写法。下面两条测试分别盯住"命名空间 URI 存在但不构成引用"
与"没有任何外部资源引用"。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness.report.html import render_report

REPO_ROOT = Path(__file__).resolve().parents[2]


def _data() -> dict:
    return {
        "suite_name": "codefix",
        "aggregate": {
            "cases": 3, "pass_rate": 0.67, "pass@k": 1.0, "flaky_rate": 0.33,
            "flaky_cases": ["bug_007"], "status_distribution": {"ok": 2, "no_finish": 1},
            "total_cost_usd": 0.42, "golden_score_mean": 0.8,
            "total_turns": 9, "total_tool_calls": 12,
        },
        "cases": [
            {"case_id": "bug_007", "tier": "medium", "status": "flaky",
             "pass_rate": 0.5, "repeats": 2, "golden_score": 0.5, "cost_usd": 0.2},
            {"case_id": "bug_008", "tier": "easy", "status": "ok",
             "pass_rate": 1.0, "repeats": 1, "golden_score": 1.0, "cost_usd": 0.22},
        ],
        "failure_modes": {"step_repetition": 2, "hallucinated_tool": 1},
        "cost_quality": [{"case_id": "a", "cost": 0.1, "pass_rate": 1.0}],
        "judge": {"consistency": 0.85, "cost_usd": 0.05, "injection_resistance": 1.0},
        "flaky": ["bug_007"],
    }


def _render(tmp_path, data: dict | None = None) -> str:
    out = render_report(data or _data(), tmp_path / "r.html")
    return out.read_text(encoding="utf-8")


# ---- 自包含 ----
def test_report_is_a_single_html_file(tmp_path):
    out = render_report(_data(), tmp_path / "nested" / "r.html")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in text


def test_no_external_resource_is_referenced(tmp_path):
    """★ 真正要测的约束：没有任何会发起网络请求的引用。

    判据是**引用型写法**，不是"出现 http 字样"—— 见模块 docstring。
    """
    text = _render(tmp_path)
    for pattern in (
        r'<script[^>]*\ssrc\s*=',
        r'<link[^>]*\shref\s*=',
        r'<img[^>]*\ssrc\s*=\s*["\']https?:',
        r'@import',
        r'url\(\s*["\']?https?:',
        r'<iframe',
    ):
        assert not re.search(pattern, text, re.I), f"报告引用了外部资源：{pattern}"


def test_echarts_is_inlined_not_linked(tmp_path):
    text = _render(tmp_path)
    assert "echarts" in text.lower()
    assert "<script src=" not in text
    # vendor 进来的源码确实在页面里（不是引擎名出现在某个字符串里）
    assert "echarts.init" in text or "echarts.version" in text


def test_vendored_echarts_contains_namespace_uris_but_is_still_self_contained(tmp_path):
    """把"为什么不能断言全文无 http://"这件事固化成测试。

    它同时是一条**回归保护**：如果哪天有人把 ECharts 换成从 CDN 加载，
    这条会因为 `<script src=` 而失败（上一条测试），而不是因为命名空间。
    """
    vendored = (REPO_ROOT / "src" / "harness" / "static" / "echarts.min.js")
    assert vendored.exists(), "ECharts 必须是 vendor 进仓库的，不能依赖 CDN"
    source = vendored.read_text(encoding="utf-8")
    assert "http://www.w3.org/2000/svg" in source, (
        "ECharts 里应当含 SVG 命名空间常量 —— 若这里失败说明换了版本，"
        "请复核 static/README.md 记的版本与哈希"
    )
    # 而页面里这些 URI 不构成资源引用
    assert not re.search(r'<script[^>]*\ssrc\s*=', _render(tmp_path), re.I)


# ---- 指标 ----
def test_all_metrics_are_rendered(tmp_path):
    text = _render(tmp_path)
    for token in ("pass_rate", "pass@k", "flaky", "0.67", "0.85"):
        assert token.lower() in text.lower(), f"{token} 没出现在报告里"


def test_the_three_outcome_metrics_are_separate_cards(tmp_path):
    """三张独立卡片，不做加权、不给总分。"""
    text = _render(tmp_path)
    assert "67.0%" in text   # pass_rate
    assert "100.0%" in text  # pass@k
    assert "33.0%" in text   # flaky_rate


def test_golden_score_shows_as_na_when_absent(tmp_path):
    data = _data()
    data["aggregate"]["golden_score_mean"] = None
    text = _render(tmp_path, data)
    assert "n/a" in text


def test_flaky_cases_are_prominently_listed(tmp_path):
    text = _render(tmp_path)
    assert "bug_007" in text
    assert "flaky cases" in text
    # 位置：flaky 一节必须在图表之前
    assert text.index("flaky cases") < text.index("chart-failure-modes")


def test_no_flaky_says_so_explicitly(tmp_path):
    data = _data()
    data["flaky"] = []
    data["aggregate"]["flaky_cases"] = []
    assert "没有 flaky 用例" in _render(tmp_path, data)


def test_failure_modes_are_rendered(tmp_path):
    text = _render(tmp_path)
    assert "step_repetition" in text
    assert "hallucinated_tool" in text


def test_case_table_lists_each_case(tmp_path):
    text = _render(tmp_path)
    assert "bug_008" in text
    assert "medium" in text and "easy" in text


def test_metric_definitions_are_in_the_footer(tmp_path):
    """口径必须跟数字一起交付 —— 数字离开口径就是噪音。"""
    text = _render(tmp_path)
    assert "无偏估计量" in text
    assert "分列" in text


# ---- 注入 ----
def test_suite_name_is_html_escaped(tmp_path):
    """Suite 名可能含特殊字符，必须转义。"""
    data = _data()
    data["suite_name"] = "<script>alert(1)</script>"
    text = _render(tmp_path, data)
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_case_id_in_the_script_block_cannot_close_it(tmp_path):
    """★ `autoescape` **管不到 `<script>` 内部** —— 这条是补它的。

    case_id 来自 suite 配置，是外部输入。嵌进脚本的 JSON 里若含
    `</script>`，脚本块会被提前闭合，后面的字符串就变成可执行的 HTML。
    """
    data = _data()
    data["cases"] = [{"case_id": "</script><script>alert(1)</script>",
                      "tier": "easy", "status": "ok", "pass_rate": 1.0,
                      "repeats": 1, "golden_score": None, "cost_usd": 0.0}]
    text = _render(tmp_path, data)

    assert "<script>alert(1)</script>" not in text

    # 脚本块里一个裸的 `<` 都不该剩 —— 整块内容因此彻底惰性
    blob = text.split("const DATA = ", 1)[1].split(";\n", 1)[0]
    assert "<" not in blob
    assert "\\u003c" in blob
    # 且 JSON 仍然可解析（< 解析回来就是 `<`，数据没变形）
    assert json.loads(blob)["cases"][0]["case_id"].startswith("</script>")


def test_failure_mode_names_are_escaped_in_the_table(tmp_path):
    data = _data()
    data["failure_modes"] = {"<img src=x onerror=alert(1)>": 1}
    text = _render(tmp_path, data)
    assert "<img src=x" not in text
    assert "&lt;img" in text


# ---- 退化输入 ----
def test_report_renders_with_empty_data(tmp_path):
    data = {"suite_name": "empty", "aggregate": {}, "failure_modes": {},
            "cost_quality": [], "judge": None, "flaky": []}
    out = render_report(data, tmp_path / "r.html")
    assert out.exists()
    assert out.stat().st_size > 1000


def test_report_renders_without_a_suite_name(tmp_path):
    data = _data()
    data["suite_name"] = ""
    assert render_report(data, tmp_path / "r.html").exists()


def test_judge_section_is_omitted_when_absent(tmp_path):
    data = _data()
    data["judge"] = None
    text = _render(tmp_path, data)
    # 用 h2 判存在性：JS 里有一句同名的注释，裸字符串判不准
    assert "<h2>judge 可靠性</h2>" not in text


def test_missing_vendored_echarts_fails_loudly(tmp_path, monkeypatch):
    """ECharts 缺失时必须报错，不能悄悄退回 CDN 引用。

    悄悄退回的话，"自包含"这个性质就在无人察觉的情况下失效了 ——
    报告在 CI 里能打开、在离线环境里打不开，而没人知道为什么。
    """
    from harness.report import html as html_mod

    monkeypatch.setattr(html_mod, "_STATIC", tmp_path / "no-such-dir")
    with pytest.raises(FileNotFoundError, match="ECharts"):
        render_report(_data(), tmp_path / "r.html")


def test_output_uses_lf_newlines(tmp_path):
    r"""Windows 上默认会把 \n 翻成 \r\n，报告体积白白变大。"""
    out = render_report(_data(), tmp_path / "r.html")
    assert b"\r\n" not in out.read_bytes()
