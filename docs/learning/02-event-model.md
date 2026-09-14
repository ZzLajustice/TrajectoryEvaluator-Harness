# 任务 2：事件模型

> **所属里程碑**：M1 · **前置任务**：任务 1 · **代码位置**：`src/harness/events/types.py`、`tests/events/test_types.py`、`tests/fixtures/event_fields.json`

## 1. 总体目标

事件模型是全系统的 **L0 契约**：评测器、store、middleware、OTel 投影全都读它。它要挡住的痛点是 **schema 漂移**（设计文档 R2）。

场景：某个评测器写着写着发现"我需要知道 provider 内部重试了几次"，最省事的做法是往 `LLMResponseEvent` 加一个字段。加完之后：① 已落盘的 golden JSONL 缺这个字段，反序列化开始依赖默认值；② 事件定义被上层业务需求倒逼修改，L0 的稳定性名存实亡；③ 半年后没人知道这个字段是给哪个评测器加的。

设计文档给了四层防线，本任务落地前两条与守卫：类型下沉 + `extra="forbid"`、`attrs` 逃生舱、字段快照测试，外加规约"加字段前先自问能否从已有事件派生"。

同时把 `CONTEXT_COMPACT` 与 `POLICY_DENY` 提升为**一等事件**：在别处它们通常只写日志，但恰恰是过程级评测最有价值的信号——上下文被压缩后丢失关键信息、越权被拦截后的降级行为，都是真实的失败模式。

## 2. 实现流程

1. 写 4 个失败测试：roundtrip 保真 / 未知 type 被拒 / `frozen` 拒改 / 额外字段被拒
2. 跑测试确认 `ModuleNotFoundError`（红）
3. 写 `types.py`（枚举 + 基类 + 各子类 + 判别联合 + `parse_event` / `dump_event`）
4. 跑测试确认 4 passed（绿）
5. **补字段快照测试** `test_schema_compat.py`，首跑自动生成 `tests/fixtures/event_fields.json`
6. commit

顺序理由：

- **第 2 步不能省。** 跳过"确认失败"就无法证明测试真的在测东西——一个 import 路径写错的测试文件也会"通过"（收集不到用例），这种假绿比没测试更危险。
- **快照测试放最后（第 5 步）而不是第一步**：快照需要"被锁定的对象"已经成型才能生成，否则你锁的是推导中的半成品，每改一次实现就要改一次 fixture，锁随即失去意义。
- **快照单独放一个文件**：它测的不是行为而是"契约形状"。混在行为测试里，未来有人重写行为测试时容易顺手删掉它。

## 3. 具体技术实现

**`ConfigDict(frozen=True, extra="forbid")` 各自解决什么**：

| 配置 | 语义 | 解决什么 |
|---|---|---|
| `frozen=True` | 实例不可变、可 hash | 事件被多个评测器与后台 writer 共享，不可变意味着不需要拷贝、不会"读到一半被改" |
| `extra="forbid"` | 未声明字段直接 `ValidationError` | schema 漂移立刻报错而非静默吞掉（R2 第 1 层防线） |

**判别联合必须显式给 discriminator**：`EventUnion = Annotated[Union[...], Field(discriminator="type")]`，子类用 `type: Literal[EventType.TOOL_RESULT] = EventType.TOOL_RESULT` 作标签。反例：不写 discriminator 时 pydantic 按 union 成员顺序逐个尝试——当两个子类字段恰好都能被对方接受时会**静默解析成错误类型**，而这种错误往往在几百行之后才炸。tech-stack §3 另记录了两条 pydantic 2.13 行为变更：序列化时不再回退尝试其他 union 成员（失败即报错，对轨迹存储是好事）、callable discriminator 在序列化阶段也会被调用。

**用 `TypeAdapter` 而不是 `BaseModel`**：`EventUnion` 是 `Annotated[Union[...]]` 而非类，没有 `.model_validate()`。所以要有模块级 `_ADAPTER = TypeAdapter(EventUnion)`，`parse_event` / `dump_event` 都走它。

**`dump_event` 必须 `mode="json"`**：`ts` 是 `datetime`，不转 JSON 模式拿到的是 datetime 对象，`json.dumps` 会直接崩。这是轨迹存储链路上第一个会踩到的坑。

**扁平子类，不套 `payload` 一层**：反例是 `Event(type=..., payload: dict)`——评测器永远只能拿到通用事件，必须 `isinstance` 收窄 + 二次解包 `ev.payload["content"]`，且 payload 内部毫无类型检查。正解是每种类型一个扁平子类，字段本身即 schema 的唯一真相源，评测器直接 `.content` / `.ok`。

**时间戳用 `default_factory=lambda: datetime.now(timezone.utc)`**：不能写 `datetime.utcnow()`（返回 naive datetime），tech-stack §11 打开的 ruff `DTZ` 规则集专门抓这个。轨迹时间戳必须带 tz，否则跨时区聚合出错。

**快照测试的要点**：遍历模块命名空间，用 `issubclass(obj, BaseModel)` + `hasattr(obj, "type")` 筛出具体事件子类（排除基类 `Event`），取 `sorted(obj.model_fields)` 组成 `{类名: [字段...]}` 与 fixture 全等比较；fixture 不存在时自动生成。**"自动生成"是刻意的**：它让这个测试能被无障碍引入已有项目，而不要求手工誊写现状。

## 4. 使用的技术栈简介

| 技术 | 说明 |
|---|---|
| `pydantic` 2.13.x | 判别联合是官方推荐做法（"more performant and more predictable than untagged unions"）；pydantic-core 是 Rust 实现，`model_dump_json()` 比 `json.dumps(model_dump())` 快很多且类型安全 |
| `TypeAdapter` | pydantic 给"非 BaseModel 类型（含 union）"的入口，提供 `validate_python` / `dump_python` |
| `StrEnum` | 3.11+ 标准库。收益是 JSON 里出现 `"tool.result"` 而非 `"EventType.TOOL_RESULT"` |
| `datetime` + `timezone.utc` | 标准库，配合 ruff `DTZ` 强制时区安全 |

替代品：`dataclass`（无校验、无 union）、`attrs`（生态更小）、`msgspec`（更快，但会引入第二套模型体系，tech-stack §3 明确不用）。**注意 pydantic 在这里不只是解析库**——它的 `extra="forbid"` 与 discriminator 正是 R2 防线的实现手段。

## 5. 工程化思想

**（1）快照测试比 code review 可靠，因为它不疲劳。** review 到第 300 行时注意力已衰减，"他加了个字段，合理"是最常见的放行理由；快照对第 1 个和第 100 个字段一视同仁。可迁移的判据：**凡是"变更需要被人注意到"的东西（schema、依赖清单、公开 API、SQL 迁移），都值得一个快照**。注意快照的正确用法是"失败即提醒"而非"失败即阻塞"——报错信息必须写清"若是有意为之，请更新 fixture 并在 PR 说明"。

**（2）逃生舱字段是接口演进的必要设计，不是妥协。** `attrs: dict[str, Any]` 表面破坏了类型安全，换来的是：想加临时字段的人有地方可去，于是不会去动正式 schema。**没有逃生舱的严格接口最终会被绕过**（改副本、塞进已有字段、monkey patch）。给临时需求一个显式的、隔离的出口，是长期接口设计的通用手法（OTel 的 span attributes、HTTP 的 `X-` 头是同一模式）。

**（3）不可变事件简化并发推理。** 事件被 store 的后台 writer、多个评测器、OTel 投影同时读取。`frozen=True` 让"这个对象会不会在别处被改"**从考虑范围里消失**，而不是靠约定保证。代价是修改要 `model_copy(update=...)` 造新对象——对一次写入的事件来说这个代价是零。

**（4）"版本化"的成本在第一天最低。** `schema_version` 从第一版就在，成本是一个字段；等到第三版再加，就变成"如何区分已落盘的两种格式"的考古工作。这个道理适用于任何会被持久化的结构。
