# StructuredOutput 结构化输出设计文档

> **模块**：`app/services/llm/structured.py`
> **职责**：从 LLM 输出中提取结构化数据（三级降级：JSON Schema → JSON Mode → 正则提取）
> **统一入口**：对外唯一入口为 `LLMService.generate_structured()`，委托 `StructuredOutput.extract()` 三级降级；`StructuredOutput` 为内部实现载体（接收完整 messages）
> **配套**：集成于 `LLMService.generate_structured()`，底层复用 `LLMService.generate()`（重试/熔断/限流），见 `llm_service.py`

---

## 目录

- [StructuredOutput 结构化输出设计文档](#structuredoutput-结构化输出设计文档)
  - [目录](#目录)
  - [设计目标](#设计目标)
    - [统一入口决策：generate\_structured 委托 extract（2026-08-07）](#统一入口决策generate_structured-委托-extract2026-08-07)
  - [核心概念解释](#核心概念解释)
    - [结构化输出（Structured Outputs）](#结构化输出structured-outputs)
    - [什么时候该使用结构化输出？](#什么时候该使用结构化输出)
    - [JSON Schema](#json-schema)
    - [JSON mode vs Structured Outputs](#json-mode-vs-structured-outputs)
    - [strict mode](#strict-mode)
    - [finish\_reason / refusal](#finish_reason--refusal)
    - [正则提取 / constrained decoding](#正则提取--constrained-decoding)
    - [三级降级](#三级降级)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [build\_json\_schema\_request — 原生 JSON Schema 请求](#build_json_schema_request--原生-json-schema-请求)
    - [build\_json\_mode\_request — JSON mode 请求](#build_json_mode_request--json-mode-请求)
    - [extract — 三级降级编排](#extract--三级降级编排)
    - [\_try\_extract — 单级提取（response\_format 形态）](#_try_extract--单级提取response_format-形态)
    - [\_fallback\_extract — 正则兜底提取（无 response\_format）](#_fallback_extract--正则兜底提取无-response_format)
  - [调用流程（generate\_structured 三级降级）](#调用流程generate_structured-三级降级)
  - [与重试/限流的分层配合](#与重试限流的分层配合)
  - [配置项清单（隐含参数）](#配置项清单隐含参数)
  - [已知边界与设计取舍](#已知边界与设计取舍)
  - [代码审核与工业级对比（问题 → 修复 → 工业对照）](#代码审核与工业级对比问题--修复--工业对照)
    - [问题 1（严重）：解析后无 Schema 校验 ✅ 已修复](#问题-1严重解析后无-schema-校验--已修复)
      - [工业级调研记录（2026-08-08 问题1）](#工业级调研记录2026-08-08-问题1)
    - [问题 2（中）：不检查 finish\_reason / refusal ⚠️ 未修复](#问题-2中不检查-finish_reason--refusal-️-未修复)
      - [工业级调研记录（2026-08-08 问题2）](#工业级调研记录2026-08-08-问题2)
    - [问题 3（中）：降级而非「错误感知重试」 ⚠️ 未修复](#问题-3中降级而非错误感知重试-️-未修复)
    - [问题 4（低）：额外字段不拒绝 ⚠️ 未修复](#问题-4低额外字段不拒绝-️-未修复)
    - [已覆盖的工业级实践](#已覆盖的工业级实践)
    - [速查表](#速查表)
  - [相关文档](#相关文档)

---

## 设计目标

1. **统一入口**：结构化输出只有一个对外入口 `generate_structured()`，调用方不散落 `json.loads(llm_result)`
2. **优先原生约束**：能走 Structured Outputs（`response_format=json_schema`）就不只依赖 prompt 或 JSON mode
3. **三级降级保证兼容**：不同模型对结构化输出支持差异大，逐级降级让廉价模型（fast）也能工作
4. **透明降级**：调用方无需知道底层用了哪级，拿到统一 dict（或 None）

### 统一入口决策：generate_structured 委托 extract（2026-08-07）

**结论：`generate_structured` 是唯一入口，内部委托 `StructuredOutput.extract` 三级降级；`StructuredOutput` 是内部实现载体。**

**背景**：此前结构化输出存在两个入口（`generate_structured` 内部自行处理 + `StructuredOutput.extract` 直接可调），职责重叠、语义不一。

| 维度 | 统一前（双入口） | 统一后（委托） |
| --- | --- | --- |
| 入口数 | 2（generate_structured 自处理 + extract） | 1（generate_structured） |
| messages 语义 | generate_structured 内部拼接 prompt | extract 接收完整 messages，透传 |
| 降级逻辑 | 分散 | 收敛到 extract 单点 |
| 调用方 | 需区分用哪个入口 | 永远用 generate_structured |

**决策依据**：

- 与 `RetryHandlerManager` 等「统一入口 + 内部实现」模式一致——调用方只面对 `LLMService`，不直接触碰内部组件
- `extract` 接收完整 messages：prompt 拼接是调用方职责，结构化模块不再假设 prompt 形状
- 降级链单点维护：未来加错误感知重试 / Schema 校验，只改 `extract` 一处

---

## 核心概念解释

### 结构化输出（Structured Outputs）

让模型按调用方提供的 JSON Schema 返回结构化对象的机制。服务商在解码层面约束模型输出，比 prompt 约束可靠得多（自然语言指令不能从解码层面阻止模型生成非法 token）。

**注意边界**：Structured Outputs 通过 ≠ 业务可用——它只能保证「结构符合约定」，仍要检查拒答、截断、finish_reason、字段语义、权限、业务规则。**结构化输出是入口约束，不是最终验收。**

### 什么时候该使用结构化输出？

**核心判断：下游程序需要直接消费模型输出时，就用结构化输出。** 它解决的是「让模型输出从自由文本变成程序可直接使用的数据」，而不是「让模型输出更准确」。

**适用场景**：

| 场景 | 示例 | 为什么用 |
| --- | --- | --- |
| 下游程序消费 | 意图识别、字段抽取、文本分类、工具参数生成 | 程序要按字段读取，自由文本无法直接解析 |
| 参数路由 | 工具调用参数、API 请求体构造 | 需要固定字段名/类型/枚举，才能映射到程序接口 |
| 多请求稳定性 | 批量抽取、多次调用的格式一致性 | 每次返回结构不同，下游无法统一处理 |
| 失败可定位 | 校验失败时要定位到具体字段 | 结构化后校验错误可精确到字段，而非「文本无法解析」 |

**不适合的场景**（此时不要用结构化输出）：

- **自由对话 / 生成式任务**：闲聊、创作、总结——输出是给用户看的文本，不需要结构化，强行套 Schema 反而限制表达
- **业务裁决**：模型可以解释规则，但不能替代规则系统。结构化输出的结果是**建议参数**，不是**执行指令**（权限、金额、库存判断必须由程序做）
- **无 schema 可言的输出**：没有固定字段结构的输出，套 Schema 是过度设计

**一句话判断**：**输出是否被程序直接消费？是 → 结构化输出；只是给人看 → 不需要。**

> **对应本项目**：`generate_structured()` 就是「程序直接消费」的入口——下游拿到统一 dict 才能做字段校验、工具路由。若调用方只是要一段文本回复，应走 `generate()` / `async_generate()`，不需要结构化约束。

### JSON Schema

描述 JSON 数据结构的规范：字段、类型、必填、枚举、范围（`minimum/maximum`）、是否允许额外字段（`additionalProperties`）。本模块第一级把整个 schema 原样传给服务商：

```json
{
  "type": "object",
  "properties": {"name": {"type": "string"}},
  "required": ["name"]
}
```

### JSON mode vs Structured Outputs

| 能力 | 主要解决什么 | 不能解决什么 |
| --- | --- | --- |
| JSON mode | 尽量返回可解析 JSON | 不保证字段符合你的 Schema |
| Structured Outputs | 按 Schema 返回结构化对象 | 不保证语义、事实、业务规则正确 |

**本项目语义**：第一级用 Structured Outputs（`json_schema`，解决结构），第二级退回 JSON mode（只保证可解析）。**JSON mode 是降级兜底，不是 Schema 保证**——这正对应三级降级的级间差异。

### strict mode

`response_format` 的 `json_schema.strict=True`：服务商在解码阶段强制输出严格匹配 Schema 的 JSON（拒绝额外字段、强制类型）。**仅部分模型支持**（如 gpt-4o-mini 以上；deepseek-chat 可能只支持 `json_object`）——这是降级链存在的根本原因。

### finish_reason / refusal

- `finish_reason`：模型停止原因（`stop` / `length` / `tool_calls`）。`length` = 输出被 max_tokens 截断，可能只剩半个 JSON 对象
- `refusal`：模型拒答字段（内容安全策略触发），此时 `content` 可能为空或含拒绝说明

**生产语义**：这两个字段是「API 边界检查」的一部分——截断、拒答与「正常返回但解析失败」是三类不同失败，处理方式不同（截断可扩大 token 重试，拒答不应强行 repair）。当前模块**未检查**（见「代码审核」问题 2）。

### 正则提取 / constrained decoding

- **正则提取**（本项目第三级）：无 response_format 时纯 prompt 约束 + 去 Markdown 代码块 + `json.loads`。所有模型可用，但可靠性最低（模型可能输出多余说明、代码块、残缺 JSON）
- **constrained decoding**（工业级，未采用）：自部署模型在解码阶段限制只能生成符合规则（JSON Schema / Regex / CFG / FSM）的 token。显著提升格式稳定，但接入复杂、对推理框架有要求、仍不保证语义正确

### 三级降级

```
第一级：原生 JSON Schema（strict=True）  —— 可靠性最高，模型要求最高
第二级：JSON Mode（json_object）          —— 只保证可解析，不保证 Schema
第三级：纯 Prompt + 正则提取（无 schema）  —— 兼容所有模型，可靠性最低
```

**为何降级而非「只用最高级」**：不同模型对结构化输出的支持差异很大。三级降级让结构化输出在廉价模型（fast）上也能工作，只是在必要时才走更低级。代价是每级失败多一次模型调用（token 消耗）。

---

## 架构总览

```
调用方
   │
   ▼
LLMService.generate_structured(messages, schema, model_key)
   │
   ▼  委托
StructuredOutput.extract(llm_service, messages, schema, model_key)
   │
   ├─ 第一级：build_json_schema_request(schema)  →  _try_extract（strict JSON Schema）
   │        失败（不支持 / 解析失败）↓
   ├─ 第二级：build_json_mode_request()           →  _try_extract（JSON mode）
   │        失败 ↓
   └─ 第三级：_fallback_extract（prompt + 正则，无 response_format）
        全失败 → 返回 None
   │
   └─ 每级内部：llm_service.generate(...)
         ├─ RetryHandlerManager（重试/熔断）
         ├─ ReservationLimiterManager（限流 reserve/settle）
         └─ StreamParser.parse_non_stream（解析完整响应）
```

**分层**：

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 统一入口 | `LLMService.generate_structured` | 对外唯一入口，委托 extract |
| 编排 | `StructuredOutput.extract` | 三级降级顺序控制（成功即返回，失败逐级下探） |
| 请求构造 | `build_json_schema_request` / `build_json_mode_request` | 构造 response_format 参数 |
| 提取 | `_try_extract` / `_fallback_extract` | 调 generate + 解析 content + 返回 dict/None |

---

## 组件详解

### build_json_schema_request — 原生 JSON Schema 请求

```python
@staticmethod
def build_json_schema_request(schema: dict[str, Any]) -> dict[str, Any]:
    """构建 response_format 参数（strict JSON Schema）。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_output",
            "strict": True,
            "schema": schema,
        },
    }
```

**要点**：

- **strict=True**：强制解码阶段匹配 Schema，字段/类型/枚举由服务商保证
- **name 固定为 `structured_output`**：服务商要求的 schema 命名
- **schema 原样透传**：调用方负责 schema 设计（字段少而明确、用枚举、禁额外字段，见「工业级对照」问题 4）

### build_json_mode_request — JSON mode 请求

```python
@staticmethod
def build_json_mode_request() -> dict[str, str]:
    """构建普通 JSON mode 请求参数（无 Schema 约束）。"""
    return {"type": "json_object"}
```

**要点**：

- 只要求输出可解析 JSON，**不含 Schema 约束**——字段/类型/枚举不做保证
- 对应工业级「JSON mode 是基础能力，不要当成 Schema 保证」

### extract — 三级降级编排

```python
@staticmethod
async def extract(llm_service, messages, schema, model_key="fast"):
    """三级降级：JSON Schema → JSON Mode → 正则提取。"""
    response_format = StructuredOutput.build_json_schema_request(schema)
    result = await StructuredOutput._try_extract(llm_service, messages, response_format, model_key)
    if result is not None:
        return result

    response_format = StructuredOutput.build_json_mode_request()
    result = await StructuredOutput._try_extract(llm_service, messages, response_format, model_key)
    if result is not None:
        return result

    return await StructuredOutput._fallback_extract(llm_service, messages, model_key)
```

**要点**：

- **顺序固定**：高约束 → 低约束，每级成功即返回，不再下探
- **级间判定是「解析成功 + Schema 校验通过」**：`_try_extract` 返回非 None dict 且通过 `_validate_schema` 才视为成功（见「代码审核」问题 1，已修复）
- **透明**：调用方不知道命中了哪级

### _try_extract — 单级提取（response_format 形态）

```python
@staticmethod
async def _try_extract(llm_service, messages, response_format, model_key):
    """尝试用指定 response_format 提取。"""
    try:
        result = await llm_service.generate(
            messages=messages, temperature=0, max_tokens=2048,
            response_format=response_format, model_key=model_key,
        )
        if result and result.content:
            parsed = json.loads(result.content)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return None
```

**要点**：

- **temperature=0**：结构化输出要确定性，禁用采样随机
- **max_tokens=2048**：硬编码输出上限（可能截断，见「代码审核」问题 2）
- **`isinstance(parsed, dict)`**：拒绝顶层是数组/标量的 JSON（列表穿不过，测试 `test_non_dict_content_returns_none` 覆盖）
- **静默吞异常**：任何异常（解析失败、下游失败）→ 返回 None → 触发下一级。**不区分失败类型**（见问题 2/3）

### _fallback_extract — 正则兜底提取（无 response_format）

```python
@staticmethod
async def _fallback_extract(llm_service, messages, model_key):
    """纯 prompt 约束降级方案。"""
    try:
        result = await llm_service.generate(
            messages=messages, temperature=0, max_tokens=2048, model_key=model_key,
        )
        if not result or not result.content:
            return None
        content = result.content.strip()
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
        content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
```

**要点**：

- **无 response_format**：走纯 prompt 约束（prompt 由调用方构建），模型可能输出解释/代码块
- **仅去 Markdown 代码块**：剥掉 ```` ```json ```` 包裹后 `json.loads`——这是「保守 repair」的典型动作
- **不修事实**：只做语法级归一化（剥代码块），不猜测意图、不补字段、不映射枚举——符合工业级「JSON repair 只能修语法，不能修事实」
- **每级一次模型调用**：三级全失败 = 3 次调用（token 消耗，见「已知边界」边界 4）

---

## 调用流程（generate_structured 三级降级）

```
generate_structured(messages, schema, model_key="fast")
    │
    └─ StructuredOutput.extract(...)
         │
         ├─ 第一级 _try_extract（response_format=json_schema, strict）
         │     ├─ generate() → 重试/熔断/限流（内部，见「分层配合」）
         │     ├─ json.loads(content) → dict → 返回 ✅（停在这里）
         │     └─ 解析失败 / 非 dict / 异常 → None → 降级
         │
         ├─ 第二级 _try_extract（response_format=json_object）
         │     ├─ 同上
         │     └─ 失败 → None → 降级
         │
         └─ 第三级 _fallback_extract（无 response_format，prompt + 正则）
               ├─ 剥 Markdown 代码块 → json.loads → dict → 返回 ✅
               └─ 失败 → None
    最终：返回 dict 或 None
```

**关键点**：

- **级级完整调用 generate**：每级都是独立完整的模型调用（含重试/限流），不是「同一响应多级解析」
- **成功短路**：高一级成功即返回，后续级别不再执行
- **全失败返回 None**：调用方需自行处理 None（降级 / 追问 / 返回 unknown，见「工业级对照」）

---

## 与重试/限流的分层配合

```
extract 三级降级          generate 内部（每级调用）
结构化约束层              可靠性层（透明，结构化模块无感知）
   │                         │
   ▼                         ▼
response_format 控制     RetryHandlerManager（重试/熔断，model_key 共享）
成功/失败判定             ReservationLimiterManager（限流 reserve/settle）
                           StreamParser.parse_non_stream（解析响应）
```

- **结构化层**（structured.py）只负责「怎么约束 + 怎么判定成功」——选 response_format、解析 content
- **可靠性层**（generate 内部）透明兜底：限流排队、重试/熔断处理下游故障——结构化模块**不感知**，每级调用自动获得
- **两者互补**：结构化降级处理「模型能力不足/输出不合规」，可靠性层处理「下游故障/配额不足」

**注意**：结构化降级的每一级都会经过完整的重试/熔断/限流——三级全失败意味着最多 3 组（每组含内部重试）模型调用。这是「降级保证兼容」的代价（见「已知边界」边界 4）。

---

## 配置项清单（隐含参数）

structured.py 无独立配置项，调用 `generate` 时使用硬编码默认参数：

| 参数 | 硬编码值 | 说明 |
| --- | --- | --- |
| `temperature` | `0` | 结构化输出确定性（禁采样随机） |
| `max_tokens` | `2048` | 输出上限（过大可能截断，见问题 2） |
| `model_key` | `fast`（可传参覆盖） | 默认用廉价快速模型，必要时传 reasoning/main |
| `response_format` | 级内构造 | 第一级 json_schema / 第二级 json_object / 第三级无 |

> **与限流的关系**：结构化模块不直接接触限流配置（RPM/TPM 由 generate 内部按 model_key 读取），但其每次调用都按 `model_key` 扣配额——见 [limiter.md](limiter.md)。

---

## 已知边界与设计取舍

1. **成功判定 = 可解析 dict + Schema 校验通过**：`_try_extract` / `_fallback_extract` 在 `json.loads` + `isinstance(dict)` 后，经 `_validate_schema`（jsonschema）校验字段类型/枚举/必填/范围，失败记日志并返回 `None` 触发降级。已修复（2026-08-08，见问题 1）。**残留边界**：校验失败直接降级，未做「错误回喂重试」（见问题 3）。
2. **不检查 finish_reason / refusal**：`length` 截断（半个 JSON → 解析失败 → 静默降级）与模型拒答（content 为空 → 静默降级）无区分、无日志。截断事实不进入任何观测（见问题 2）。
3. **降级而非「错误感知重试」**：第一级失败后直接换更弱的约束方式，**不把校验错误回喂给模型重试**。对 cheap 模型，回喂错误往往是唯一有效纠错手段（见问题 3）。
4. **多级降级 = 多次模型调用**：三级全失败 = 3 组调用（含各组的重试），token 消耗放大。这是「兼容所有模型」的显式代价——换取廉价模型可用性，而非默认接受解析失败。
5. **每级调用 `max_tokens=2048` 硬编码**：结构化输出通常较短，2048 是合理默认；但长字段（如大段抽取）可能截断且不感知（见边界 2）。
6. **额外字段不拒绝**：`extract` 接收任意 schema 原样透传，不强制 `additionalProperties:false`。调用方 schema 若未写死，模型可能扩展字段混入（见问题 4）。
7. **吞异常无区分**：`_try_extract` 的 `except Exception: pass` 不区分失败类型——API 故障、解析失败、下游异常都归为「降级」。与可靠性层（重试/熔断/限流在 generate 内部）职责边界清晰，但**结构化层自身的失败原因不可观测**。

---

## 代码审核与工业级对比（问题 → 修复 → 工业对照）

> 本节以 2026-08-07 结构化输出模块审核发现的问题为主线，逐条记录**问题 → 现状 → 工业级对照**（对照《大模型稳定输出 JSON 的生产级方案：从结构化输出到验收闭环》zhuwei.fun，调研日期 2026-08-07）。修复状态逐条标注（⚠️ 未修复 / ✅ 已覆盖）。完整速览见文末[速查表](#速查表)。

### 问题 1（严重）：解析后无 Schema 校验 ✅ 已修复

**位置**：`_try_extract` / `_fallback_extract`（`json.loads` 后加 `_validate_schema` 校验）

**已修复（2026-08-08）**：新增 `_validate_schema`（基于 `jsonschema` 库 `validate()`），`_try_extract` / `_fallback_extract` 在解析出 dict 后按 schema 校验；校验失败记 `logger.warning`（含 schema 与解析结果）→ 返回 `None` → 触发降级。三级降级每一级都带校验兜底，不再返回「结构合法但值非法」的坏数据。修复前：Schema 上要求 `confidence` 0~1，模型返回 `{"confidence": "很高"}` 也会当成功返回，上层信以为真，**系统不知道自己不稳定**。

**修复要点**：

- **jsonschema 选型**：schema 是 JSON Schema dict（非 Pydantic model），`jsonschema` 零转换直接校验，与调研结论一致（Pydantic 作者官方定位：JSON Schema 实例校验归 jsonschema）
- **strict 只锁结构，本地校验锁值**：`confidence` 0~1 这类 `minimum`/`maximum` 值约束 strict 不保证，`_validate_schema` 兜底（服务端 strict 锁结构 + 本地 jsonschema 锁值，双保险）
- **校验失败触发降级**：与现有三级降级链衔接——校验失败 = 该级无效 → 降级下一级，而非返回坏数据
- **非法 schema 有防护**：`validate()` 抛非 `ValidationError` 异常（schema 本身非法）也记日志返回 False，不静默穿透
- **遗留（不属于问题 1）**：校验失败直接降级，未做「错误回喂重试」（见问题 3）

**工业级原则**：模型返回之后不能直接进业务逻辑，生产链路必须有 Schema 校验一环：

```
模型响应 → 检查 API 状态和 finish_reason → 解析 → Schema 校验
→ 语义置信度与缺失字段判断 → 业务规则校验 → 通过后进入业务系统
```

> 程序校验的意义不是"让模型更稳定"，而是**让系统知道模型什么时候不稳定**。

#### 工业级对照：程序校验是必需品

Pydantic 统一校验（`ConfigDict(extra="forbid")` + `model_validate_json`）：

```python
class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["query_order", "cancel_order", "refund_order", "unknown"]
    order_id: str | None
    confidence: float = Field(ge=0, le=1)

def validate_llm_output(raw_json: str) -> IntentResult:
    return IntentResult.model_validate_json(raw_json)
```

**改进方向**（建议）：`extract` 增加可选 `validator`（或内置 JSON Schema 校验器）。解析后校验字段类型/枚举/必填；校验失败 → 记日志 + 返回 `None`（触发降级），而非返回坏数据。这会让降级链真正有意义——**当前第一级的 strict 约束一旦降级到 JSON mode/正则，Schema 保证就丢了，而后续级别恰好没有校验兜底**。

#### 工业级调研记录（2026-08-08 问题1）

> 调研目的：修复问题 1（解析后无 Schema 校验）前，先查证工业级项目如何解决「模型返回 JSON 后的 Schema 校验」。调研对象：OpenAI/Anthropic 官方 Structured Outputs、Instructor、LangChain、Guardrails AI、Outlines/JSONFormer、Vellum 及 jsonschema vs Pydantic 选型讨论。

**结论先行**：

1. **双保险是业界共识**：即使启用服务端 strict mode，也必须在客户端再做一次本地校验。strict 只锁「结构」（字段存在、类型、enum、无多余字段），**不锁「值」**——`minimum`/`maximum`/`pattern`/`format` 等校验关键字不被服务端保证；且 refusal、`finish_reason="length"` 截断会绕过 schema。典型例：`confidence: "很高"` 在 strict 下仍会通过（strict 只保证它是 float，不保证在 [0,1]）。
2. **校验失败的主路径是「带错误回喂重试（1~3 次）→ 失败后再降级」**，而非直接降级/默认值。要点：必须把 `ValidationError` 转成人话 + 模型上一次的输出一起回喂（**绝不盲目同 prompt 重试**）；按失败类别分流——结构/参数错（缺字段、类型错）可回喂重试（模型能自纠），业务语义错不重试直接降级。
3. **校验库选型：schema 是 JSON Schema dict → 直接用 `jsonschema` 库**。Pydantic 作者官方表态「JSON Schema 实例校验是 jsonschema 的事，Pydantic 不做」，`TypeAdapter` 内部也是接 jsonschema。仅当「校验后立刻拿类型化对象 + 跨字段 validator」时才值得转 Pydantic。

**工业级方案速览**：

| 方案 | 校验策略 | 校验失败处理 |
| --- | --- | --- |
| OpenAI/Anthropic Strict Outputs | 服务端约束解码（解码层禁止非法 token） | 官方客户端不替你做业务校验；refusal/截断自行分支；官方仍建议本地 Pydantic `model_validate()` 回读 |
| Instructor（Pydantic 标杆） | 客户端 4 层校验（JSON 解析 / Schema / 字段 validator / 跨字段 model validator） | reask：错误格式化后追加进对话历史重生成，`max_retries` 默认 3，耗尽抛 `InstructorRetryException`（含 `failed_attempts` 供监控） |
| LangChain | 传 Pydantic 类才校验（实例化时）；**传 JSON Schema dict 默认不校验**（与本项目现状同缺口） | `include_raw=True` 返回 `{raw, parsed, parsing_error}` 便于接重试 runnable；`strict` 参数控制是否校验 |
| Guardrails AI | 独立于模型的 Validators（schema + 内容质量） | OnFailAction 决策表：REASK（限次）/ FIX / FIX_REASK / FILTER / REFRAIN / EXCEPTION / NOOP；总失败暴露 `validation_passed` |
| Outlines / JSONFormer（约束解码派） | 生成层 FSM/类型生成器，结构合法是「数学上不可能违反」 | 结构层不失败（除非截断）；作者警告只保形状不保语义，仍需内容层校验兜底 |

**选型对照（jsonschema vs Pydantic）**：

| 维度 | `jsonschema` 库 | Pydantic |
| --- | --- | --- |
| 输入 | JSON Schema dict 直接校验，零转换 | 需先定义 Model 或 `TypeAdapter`（非一等公民） |
| 适用 schema | schema 是 dict、动态生成、跨语言契约 | schema 静态、写死在 Python 类型里 |
| 校验能力 | 完整 JSON Schema 语义（`anyOf`/`oneOf`/`$ref`/format） | 类型 + 自定义 validator + 跨字段逻辑 |
| 产物 | dict（原样返回） | 类型化对象 |
| 官方定位 | Pydantic 作者：JSON Schema 实例校验归 jsonschema | 针对不可信数据的类型化校验 |

**对本项目的启示**：

- 接口 `generate_structured(messages, schema_dict, ...)` 的 schema 是 **JSON Schema dict**、不引 agent 框架 → 与调研结论完全吻合：**用 `jsonschema.validate()` 做本地校验**是问题 1 的最小正确修法，零模型映射成本。
- 校验失败后「先回喂重试再降级」是工业级主路径，但这对应 structure.md 的**问题 3（错误感知重试）**，不在问题 1 范围。**本次问题 1 只做：加 `jsonschema` 校验 → 校验失败记日志 → 返回 `None` 触发降级**；「回喂重试」留给问题 3 作为独立迭代。
- 兼容 API 若支持 strict mode，则为双保险：服务端锁结构 + `jsonschema` 锁值（范围/format 等 strict 不保证的部分）；不支持 strict 时 `jsonschema` 是唯一结构防线，优先级更高。
- 参照实现：jsonschema 官方库、Pydantic `TypeAdapter`（内部接 jsonschema）。

**信息来源**：

- OpenAI strict mode 仍需客户端校验：<https://community.openai.com/t/strict-mode-does-not-enforce-the-json-schema/1104630>、<https://www.respan.ai/articles/openai-structured-outputs-vs-json-mode>
- Instructor reask/校验：<https://python.useinstructor.com/concepts/reask_validation/>、错误未回喂 bug：<https://github.com/567-labs/instructor/issues/1736>
- LangChain with_structured_output（JSON Schema 不校验）：<https://reference.langchain.com/python/langchain-core/output_parsers/pydantic/PydanticOutputParser>
- Guardrails AI OnFailAction：<https://guardrailsai.com/guardrails/docs/concepts/error_remediation>
- Outlines 约束解码：<https://deepwiki.com/dottxt-ai/outlines/3-structured-generation>
- jsonschema vs Pydantic 选型：<https://www.glukhov.org/llm-performance/benchmarks/llm-structured-output-validation-python>、Pydantic 官方讨论：<https://github.com/pydantic/pydantic/discussions/5135>

### 问题 2（中）：不检查 finish_reason / refusal ⚠️ 未修复

**位置**：`_try_extract` / `_fallback_extract`（只看 `result.content` 非空）

**现状**：

- `finish_reason="length"`（max_tokens=2048 截断出半个 JSON）→ `json.loads` 失败 → **静默降级**，截断事实不进任何日志
- 模型拒答（`refusal` 字段）→ content 为空 → **静默降级**，拒答被当成「普通失败」

#### 工业级对照：失败处理要覆盖 API 边界

| 失败类型 | 典型表现 | 处理方式 |
| --- | --- | --- |
| API 调用失败 | 超时、429、5xx | 指数退避、限流、降级（本项目由可靠性层覆盖 ✅） |
| **模型拒答** | 返回 refusal 或安全拒绝 | **不强行 repair**，走安全兜底 |
| **输出截断** | finish_reason 异常或内容不完整 | 扩大 token、缩短输入、重试 |
| JSON 语法错误 | 解析失败 | 保守 repair 或错误感知重试 |
| Schema 失败 | 字段缺失、类型错误、枚举非法 | 带校验错误重试 |
| 语义低置信 | confidence 低、缺失关键字段 | 追问用户或转人工 |
| 业务校验失败 | 订单不可退、权限不足 | 返回业务原因，不让模型裁决 |

**改进方向**（建议）：`_try_extract` 里检查 `finish_reason == "length"` 或 `refusal` 非空 → 视为失败并区分日志（截断 vs 拒答），不进错误结果。截断与拒答是不同性质失败，应可观测、可分别处理。

#### 工业级调研记录（2026-08-08 问题2）

> 调研目的：修复问题 2（不检查 finish_reason / refusal）前，先查证工业级项目如何处理「输出截断」与「模型拒答」两类 API 边界失败。调研对象：OpenAI / Anthropic / DeepSeek 官方语义、instructor、LiteLLM 网关归一化、openclaw、pi-refusal-guard 等。

**结论先行**：

1. **截断与拒答是两种必须显式区分的失败**，业界检查顺序固定为：`finish_reason` → `refusal` → `content` 存在性 → 解析 → schema 校验 → 业务校验。**任何解析动作之前必须先查 `finish_reason`**。
2. **截断（`length`/`max_tokens`）**：盲重试是反模式——instructor PR #2232 记录真实事故（截断输出拼回 prompt 重试，Gemini 烧掉约 150 万 token），**绝不把截断输出拼回 prompt**。正确做法是**本层内扩 max_tokens 重试至多 1 次**，仍失败记硬失败。`length` 时 HTTP 200 也要自查 finish_reason，不能靠状态码。
3. **拒答（`refusal`/`content_filter`）**：工业共识是**拒答是「路由决策」而非「可捕获异常」**（HTTP 200 的"正常返回"）。**不强行 repair、不盲目降级重试**（分类器刻意调保守，对抗被反对）。正确姿势是短路 + 记日志（含 category）+ 返回可区分信号（unknown/转人工/安全提示）。Anthropic 特殊要求：收到 `stop_reason:"refusal"` 后必须重置会话上下文再继续。

**finish_reason 语义表**：

| 提供商 | 截断 | 拒答/过滤 | 其它 |
| --- | --- | --- | --- |
| OpenAI | `length`（token 用尽，JSON 必残缺） | `content_filter`（内容被过滤移除） | `stop` / `tool_calls` |
| Anthropic | `max_tokens`（官方建议：截断且以不完整 tool_use 结尾时用更大 max_tokens 重试） | `stop_reason:"refusal"`（Claude 4+，带 `stop_details{type, category, explanation}`） | `end_turn` / `stop_sequence` / `tool_use` |
| DeepSeek | `length`（同 OpenAI）+ 额外 `insufficient_system_resource`（推理资源中断，需整体重发） | **无 refusal 字段**；拒答只能靠「content 空 + finish_reason 异常」推断 | `stop` / `tool_calls` / `content_filter` |

**refusal 字段支持度**：

| 提供商 | 支持 refusal 字段 | 拒答信号形态 |
| --- | --- | --- |
| OpenAI | ✅ | `message.refusal` 字符串，同时 `content` 可置 `null`；官方 structured outputs 文档示例 `if result.refusal: ...` |
| Anthropic | ✅（Claude 4+） | `stop_reason:"refusal"` + `stop_details`（具名 category 如 cyber/bio） |
| DeepSeek | ❌ | 只能靠「content 空 + finish_reason 异常」推断 |

> 关键坑：OpenAI 把拒答文本放进 `refusal` 字段正是为了**不破坏 JSON 解析**——但代码若只读 `content`，就会拿到 `null`/空串 → 静默吞掉。这正是本项目当前缺陷。兼容层（LiteLLM 等）把各提供商非标准值归一化为 OpenAI 枚举，说明「内容被过滤」最终都落成 `content_filter` 或等价信号。

**对本项目的启示（最小修法，分层）**：

**第 1 层 — 数据透传（3 处小点）**：

1. `StreamResult` 增加 `self.refusal: str | None = None`
2. `parse_non_stream` 返回值加 `"refusal": getattr(msg, "refusal", None)`——**不能像 content 那样 `or ""`**，要保留 None 与空串的区分
3. `parse_chunk` 增加 `delta.refusal` 提取（OpenAI 流式拒答形态）
4. `llm_service.py` 非流式路径把 `parsed.get("refusal")` 填进 `StreamResult`

**第 2 层 — 语义决策点（structured.py，唯一决策点）**：`_try_extract` 在 `json.loads` **之前**做三态检查：

| 判定 | 条件（按序） | 动作 |
| --- | --- | --- |
| 截断 | `finish_reason == "length"`（及 `max_tokens` / `insufficient_system_resource`） | **本层内**扩 max_tokens（如 2048→4096）重试 1 次；仍截断 → 记截断日志、返回截断失败（不进入降级链） |
| 拒答/过滤 | `refusal` 非空，或 `finish_reason == "content_filter"`，或 content 空且 finish_reason 异常 | **短路**：不降级、不 repair，返回可区分信号让上层转人工/安全提示/unknown，日志带 refusal 文本与 category |
| 正常 | 其余 | 继续 `json.loads` + 已有 `_validate_schema` |

**三个关键决策**：

- **扩 max_tokens 重试放 structured 层**（`_try_extract` 内部），不放 generate/retry 层。理由：`retry.py` 是传输层（网络/限流/熔断），对「这是结构化输出」无感知；扩 token 是**业务语义重试**，只有 structured 层知道 `max_tokens=2048` 是自己设的、知道 JSON 截断可安全加大预算重试。放这里对 generate 其它调用方（Agent 聊天流）零影响。
- **拒答不走三级降级链**：降级链解决「能力缺口」（provider 不支持某种 response_format）；拒答是**策略信号**，把同一段可能触发安全的输入喂给更宽松约束，大概率同样拒答，只有成本与日志噪音。
- **截断也不走降级链**：截断是「token 预算/输出长度」问题，与 response_format 支持度正交；同一输入在 json_mode 下同样会截断。降级无益，扩 token 重试 1 次是唯一有意义的缓解。

**两个附带发现**：

- **DeepSeek 官方 API 不支持 `json_schema` 类型**（返回 400），本项目第一级（strict json_schema）对 DeepSeek 每次都白打一次请求——可选优化：按 model_key/provider 预判跳过第一级直接进 json_mode。
- **DeepSeek 无 refusal 字段**，拒答形态是「content 空 + finish_reason 异常」——拒答判定必须覆盖「content 为空且非截断」这一形态，不能只查字段。

**信息来源**：

- OpenAI finish_reason 枚举：<https://developers.openai.com/api/reference/resources/chat>；refusal 检查示例：<https://developers.openai.com/api/docs/guides/structured-outputs>
- Anthropic 停止原因处理：<https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons>；流式拒答（stop_details/context reset）：<https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/handle-streaming-refusals>
- DeepSeek finish_reason：<https://theneuralbase.com/deepseek-api/learn/beginner/finish-reason-interpretation/>；不支持 json_schema：<https://github.com/BerriAI/litellm/issues/7580>、<https://github.com/deepseek-ai/DeepSeek-V3/issues/302>
- instructor 解析前先查 finish_reason（token 烧毁事故）：<https://github.com/567-labs/instructor/pull/2232>
- OpenAI SDK LengthFinishReasonError 仅在 .parse() 抛出：<https://github.com/openai/openai-python/issues/1700>
- openclaw refusal 字段被丢弃的 bug（流式 delta.refusal 形态）：<https://github.com/openclaw/openclaw/issues/102321>
- pi-refusal-guard（拒答即路由决策、Anthropic 具名 category）：<https://github.com/LoneExile/pi-refusal-guard>
- 结构化输出生产管线检查顺序：<https://nbility.ai/blog/en/structured-output-json-schema>

### 问题 3（中）：降级而非「错误感知重试」 ⚠️ 未修复

**位置**：`extract`（失败即换约束方式，不回喂错误）

**现状**：第一级 schema 失败 → 直接降级到 JSON mode（约束更弱）→ 失败 → 正则（约束最弱）。**三级都在「换约束」，没有一次「带错误重试」**。

#### 工业级对照：把具体校验错误回喂给模型

错误感知重试不应只说"你错了"，要把具体错误 + 允许枚举值回喂：

```
你的上一次输出没有通过 JSON Schema 校验。
错误：字段 intent 的值 "退款" 不在允许枚举中。
允许值：["query_order", "cancel_order", "refund_order", "unknown"]
请只返回修正后的 JSON。不要输出解释。不要使用 Markdown。
```

重试次数有限制（通常 `max_retries = 2~3`），超限后进入降级路径（返回 unknown / 追问 / 转人工 / 普通文本 / 进离线分析队列）。

**改进方向**（建议，范围大）：schema 校验失败时把错误详情回喂模型再试一次，而非直接降级。这是对 cheap 模型最有效的纠错手段——降级到更弱约束往往仍会失败，而错误回喂直接针对缺陷修正。

### 问题 4（低）：额外字段不拒绝 ⚠️ 未修复

**位置**：`extract`（schema 原样透传，不强制 `additionalProperties:false`）

**现状**：调用方 schema 若未写 `additionalProperties:false`，模型可能扩展字段混入系统（如业务不需要的 `user_emotion`）。

#### 工业级对照：禁止额外字段

Schema 层面明确 `additionalProperties: false`，Pydantic 侧用 `ConfigDict(extra="forbid")` 双保险。意义是**减少模型自作主张扩展接口**。

**改进方向**（建议）：文档层面在 llm.md 的 schema 使用示例中引导调用方写 `additionalProperties:false`；若要做强制，可在 `extract` 对 schema 校验（或默认补全 `additionalProperties:false`）。

### 已覆盖的工业级实践

| 工业级要求 | 本项目状态 |
| --- | --- |
| 统一组件封装，不散落 `json.loads` | ✅ `generate_structured()` 统一入口 |
| 能用 Structured Outputs 不用 JSON mode | ✅ 第一级 `json_schema(strict=True)` |
| JSON mode 不做 Schema 保证 | ✅ 定位为第二级降级 |
| repair 只修语法不修事实 | ✅ 第三级仅剥 Markdown 代码块，未引入 json-repair 库 |
| API 边界失败（超时/429/5xx） | ✅ 可靠性层（retry/限流/熔断）透明覆盖 |
| 审计日志 | ✅ `llm_call` 业务事件（请求/用量/耗时，generate 内部） |
| 模型输出当不可信输入 | ✅ 三级降级 + 重试 + 熔断的整体设计意图 |
| **程序校验（Schema）** | ✅ `_validate_schema`（jsonschema）本地校验，三级降级全覆盖（问题 1） |
| **API 边界检查（refusal/截断）** | ⚠️ 缺失（问题 2） |
| **错误感知重试** | ⚠️ 缺失（问题 3） |
| **禁额外字段** | ⚠️ 未强制（问题 4） |

> **超出本模块范围**（属于 Agent 层与上层业务，structured 模块不负责）：语义/业务正确、工具执行权在后端、权限/幂等键、评测集闭环、SFT。这些由 Agent 循环与业务规则承载。

### 速查表

| 我们的缺陷 | 工业级正确做法 | 我们的现状 | 参照实现 | 结论 |
| --- | --- | --- | --- | --- |
| 解析后无 Schema 校验 | 程序校验是必需品（本地 jsonschema/Pydantic 校验） | ✅ `_validate_schema`（jsonschema）三级全覆盖，校验失败→记日志→降级 | zhuwei.fun 生产级方案 · jsonschema 库 | ✅ 已修复（2026-08-08），回喂重试留待问题 3 |
| 不检查 finish_reason/refusal | API 边界检查：截断扩 token 重试、拒答走安全兜底 | 只看 content 非空，截断/拒答静默降级 | OpenAI 文档 · zhuwei.fun 失败处理表 | ⚠️ 中，建议区分日志 |
| 降级而非错误感知重试 | 带具体校验错误回喂模型，`max_retries=2~3` 后进降级路径 | 只换约束方式，不回喂错误 | zhuwei.fun 错误感知 retry | ⚠️ 中，范围大留待后续 |
| 额外字段不拒绝 | `additionalProperties:false` + Pydantic `extra="forbid"` | schema 原样透传 | JSON Schema 规范 | ⚠️ 低，建议文档引导 |
| 多级降级 = 多次调用 | 降级路径是兜底不是默认路径 | 三级全失败 3 组调用 | — | ✅ 设计取舍，文档记录 |
| 吞异常无区分 | 失败类型可观测、分别处理 | `except Exception: pass` | zhuwei.fun 失败分类表 | ⚠️ 低，配合问题 2 改善 |

---

## 相关文档

- [llm.md](llm.md)（LLM 层总览，含「结构化输出：三级降级 vs 单一方式」对比）
- [retry.md](retry.md)（可靠性层：重试/熔断，generate 内部对结构化调用透明生效）
- [limiter.md](limiter.md)（限流层：reserve/settle，按 model_key 扣配额）
- [streaming.md](streaming.md)（响应解析：`parse_non_stream` 供 generate 使用）
