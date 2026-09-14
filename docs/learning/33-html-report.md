# 任务 33：HTML 报告

> **所属里程碑**：M8 · **前置任务**：32（聚合指标与终端报告的渲染顺序） · **代码位置**：`src/harness/report/html.py`、`src/harness/report/templates/report.html.j2`、`src/harness/static/`

## 1. 总体目标

把聚合结果变成**可交付物**。终端输出在 CI 里滚过去就没了，评审、答辩、跨团队沟通需要一个能传阅、能离线打开、能深链到具体步骤的报告。

核心约束只有一条：**完全自包含的单文件 HTML**。ECharts 内联进 `<script>`，没有任何外部 URL，没有 `<script src=`。这不只是图方便：

- **可信与合规**：报告里含模型输出与轨迹片段，加载外部 CDN 等于把这些内容暴露给第三方；
- **可用性**：CI artifact、邮件附件、内网机器、断网评审现场——任何一个场景下外链都会让报告变成半张白纸。

代价是单文件从约 400 KB 涨到约 1.5 MB（tech-stack §7.2），对上述场景完全可接受。

## 2. 实现流程

1. **vendor ECharts**：`curl` 下载并锁定版本到 `src/harness/static/echarts.min.js`，同时写 `static/README.md` 记录版本号、下载 URL、升级步骤、为什么不用 npm 依赖。
2. 编写 `report.html.j2` 模板与 `render_report()` 渲染器。
3. 跑自动化测试（自包含性、无外部 URL、转义、空数据可渲染）。
4. **手工查看报告**：`harness report --runs runs/ --format html`，用 DevTools Network 面板确认请求为空。

顺序的理由：

- vendor 必须最先做，因为「内联」的实现依赖这个文件存在——先有静态资源，才谈得上内联。
- **手工验收必须保留在清单里**。自动化测试只能证明「没有外部引用」「字符串被转义」，**不能证明图表真的画出来了**。断言 HTML 里含 `echarts` 字符串与「热力图渲染正确」之间隔着整个浏览器。假装测试覆盖了渲染正确性是自欺，清单里保留人工步骤不是流程冗余。

## 3. 具体技术实现

### 3.1 autoescape 必须开启

```python
env = Environment(loader=FileSystemLoader(str(_TEMPLATES)),
                  autoescape=select_autoescape(["html", "j2"]),   # ★ 必须开启
                  trim_blocks=True, lstrip_blocks=True)
```

测试直接钉住这条：

```python
data["suite_name"] = "<script>alert(1)</script>"
assert "<script>alert(1)</script>" not in text
assert "&lt;script&gt;" in text
```

这不是理论风险。报告里的 `suite_name`、`case_id`、failure mode 的 message、工具输出片段——**都可能来自模型输出或用户配置**。当模型输出进入渲染层，XSS 从「可能」变成「必然」：模型吐出一个 `<` 是家常便饭。

`trim_blocks` / `lstrip_blocks` 只是让 `{% %}` 不留下空行——但对「生成的 HTML 要能读、能 diff」有意义。

### 3.2 ECharts 内联

```python
echarts = (_STATIC / "echarts.min.js").read_text(encoding="utf-8")
html = env.get_template("report.html.j2").render(**data, echarts_source=echarts, ...)
```

模板里用 `{{ echarts_source | safe }}` 塞进 `<script>`。测试断言 `"<script src=" not in text`——把「不许外链」变成一条可执行契约，而不是文档里的约定。

### 3.3 四张图与三张卡片

| 图表 | 类型 | 备注 |
|---|---|---|
| 失败模式分布 | 柱状 | 数据来自任务 30 的 `category` |
| 成本-性能散点 | 散点 | 每 case 一个点（cost × pass_rate） |
| judge 可靠性 | 雷达图 | 任务 35 的 consistency / cost / injection resistance |
| 模型×任务矩阵 | 热力图 | **数据不足时隐藏** |

热力图与雷达图正是选 ECharts 而非 Chart.js 的核心理由（见 §4）。

「数据不足时隐藏」是必要的：空图表比没有图表更糟，它看起来像「没有问题」。

指标呈现沿用任务 32 的分列原则：`pass_rate` / `pass@k` / `flaky_rate` **三张独立卡片**，flaky case **单独一节居中突出**，不与聚合指标混排。

### 3.4 两个容易踩的坑

**autoescape 与 JS 数据的冲突。** `chart_data` 是 `json.dumps(..., ensure_ascii=False)` 的字符串，模板里必须 `| safe`——否则 `"` 变成 `&quot;`，JS 直接语法错误。这意味着**数据区是主动豁免转义的**，必须清楚知道豁免了哪几处、为什么。

配套的注意点：若某个 case_id 或工具输出里出现字面量 `</script>`，会提前闭合 script 标签。更稳的写法是转义为 `<\/script>`，或改用 `<script type="application/json">` + `JSON.parse`。现有测试只断言了「suite 名被转义」与「无外部 URL」，覆盖不到这条路径——报告的数据源里恰恰包含模型输出。

**vendor 资源必须留溯源记录。** `static/README.md` 要写清版本号、下载 URL、升级步骤。vendored 的第三方 JS 不在包管理器的视野里，不写下来，半年后没人知道这个 1 MB 文件是什么版本、从哪来、怎么升。内联第三方代码等于把它纳入你的产物，供应链视角下必须可追溯。

## 4. 使用的技术栈简介

### Jinja2（模板引擎）

`Jinja2` 3.1.6 发布于 2025-03，仓库最后提交 2025-06-14，此后无活动——**事实上的维护模式**。仍然选它：成熟、稳定、生态最广（deepeval core dep、ragas、lm-eval、promptfoo 的 TS 侧 nunjucks 同源）。

替代品 `minijinja` 2.24.0（mitsuhiko 本人的 Rust 实现 Python binding）速度快、零依赖，但自述 *"An experimental Python binding"*，且**模板语法与 Jinja2 有差异**（沙箱 / autoescape 语义不同）。本项目的 HTML 报告用 Jinja2 更稳，只有渲染成为性能瓶颈时才考虑 minijinja。

### ECharts（图表库）

实测数据（tech-stack §7.2）：

| 库 | 版本 | min bundle | 最后更新 | 判断 |
|---|---|---|---|---|
| Chart.js | 4.5.1 | 203 KB | 2025-10（11 个月停更） | 体积最小但半停更 |
| **ECharts** | **6.1.0** | **1095 KB** | 2026-05（活跃） | **采用** |
| Vega-Lite | 6.4.3 | 244 KB + 需 vega runtime | 2026-05 | 两段运行时，默认从 CDN 取 |
| Recharts | 3 | 579 KB | — | 依赖 React |
| Plotly.js | 4.1.0 | 4191 KB | 2026-09 | 体积与离线单文件场景不匹配 |

**选 ECharts 而非 Chart.js 的三个理由**：① 评测报告的典型图表 Chart.js 做不了——模型×任务**热力图**、judge 可靠性**雷达图**、工具调用**桑基图**都需要 ECharts 原生能力或 Chart.js 插件；② 维护活跃度（Chart.js 停在 4.5.1，ECharts 仍发版）；③ Apache-2.0，中文文档与国内生态（国内看板/报告的事实标准）。

真实项目参考不能直接照搬：promptfoo 的 Web UI 用 `chart.js` + `recharts`（它是 Web 应用不是单文件），inspect_ai 的 viewer 是独立 TS 子模块预构建后随 wheel 分发——**两者都不走单文件内联路线**。

`echarts.min.js` 作为 package data 存进 `src/harness/static/`，由 Jinja2 读文件内联，避免运行时联网。构建侧靠 `hatchling` 的 `force-include` 把它打进 wheel（tech-stack §10.2：`uv_build` 不支持动态元数据与 package data，这正是选 `hatchling` + `hatch-vcs` 的原因）。

## 5. 工程化思想

**产物自包含 = 可交付性。** 凡是「要离开生成环境」的产物（报告、导出文件、备份），都不要依赖运行时外部资源。检验标准很朴素：断网、换一台机器、三年后再打开，它还能用吗。

**不被包管理器管的依赖最危险。** vendored 资源必须带溯源记录（版本 + URL + 升级步骤），因为没有任何工具会替你追踪它。这是供应链安全里最容易被忽略的一格。

**安全默认 + 显式豁免。** 正确姿势是默认全转义、需要时逐点 `| safe` 并说明理由；错误姿势是默认不转义、靠人记得转义。豁免点必须**少且显眼**——本任务一共两处（ECharts 源码、图表数据 JSON），都能在模板里一眼找出来。

**自动断言与人工验收要分工明确。** 测试证明「结构正确」（无外部 URL、转义生效、空数据不崩），人眼证明「渲染正确」（图表真的画出来了）。把「测试全绿」当成「功能可用」是这两件事最常见的混淆。

**用户可控字符串 + 模型输出进入渲染层 ⇒ 注入是必然而非可能。** 任何把「外部文本」拼进「有语法的载体」（HTML、SQL、shell、模板）的地方，都要先问一句：这个载体的转义机制开了吗？
