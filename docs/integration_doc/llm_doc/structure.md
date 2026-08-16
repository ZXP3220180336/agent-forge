# StructuredOutput 结构化输出设计文档

> **模块**：`app/integration/llm/structured.py`
> **更新日期**：2026-08-16
> **职责**：从 LLM 输出中提取结构化数据（三级降级：JSON Schema → JSON Mode → 正则提取）
> **状态**：✅ 已实现
> **统一入口**：对外唯一入口为 `LLMService.generate_structured()`，委托 `StructuredOutput.extract()` 三级降级；`StructuredOutput` 为内部实现载体（接收完整 messages）
> **配套**：集成于 `LLMService.generate_structured()`（`app/integration/llm/llm_service.py`），底层复用 `LLMService.generate()`（重试/熔断/限流）

---

## 📋 目录

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
    - [\_build\_json\_schema\_request — 原生 JSON Schema 请求](#_build_json_schema_request--原生-json-schema-请求)
    - [\_build\_json\_mode\_request — JSON mode 请求](#_build_json_mode_request--json-mode-请求)
    - [extract — 三级降级编排](#extract--三级降级编排)
    - [\_try\_extract — 单级提取（response\_format 形态）](#_try_extract--单级提取response_format-形态)
    - [\_fallback\_extract — 正则兜底提取（无 response\_format）](#_fallback_extract--正则兜底提取无-response_format)
  - [调用流程（generate\_structured 三级降级）](#调用流程generate_structured-三级降级)
  - [与重试/限流的分层配合](#与重试限流的分层配合)
  - [配置项清单（隐含参数）](#配置项清单隐含参数)
  - [已知边界与设计取舍](#已知边界与设计取舍)
  - [问题记录](#问题记录)
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

**生产语义**：这两个字段是「API 边界检查」的一部分——截断、拒答与「正常返回但解析失败」是三类不同失败，处理方式不同（截断可扩大 token 重试，拒答不应强行 repair）。当前模块**已检查**（`_classify_result` 三态分类，见 [问题 2](../../../issues/integration/llm/2026-08-08-finish-reason-refusal-unchecked.md)）。

### 正则提取 / constrained decoding

- **正则提取**（本项目第三级）：无 response_format 时纯 prompt 约束 + 去 Markdown 代码块 + `json.loads`。所有模型可用，但可靠性最低（模型可能输出多余说明、代码块、残缺 JSON）
- **constrained decoding**（工业级，未采用）：自部署模型在解码阶段限制只能生成符合规则（JSON Schema / Regex / CFG / FSM）的 token。显著提升格式稳定，但接入复杂、对推理框架有要求、仍不保证语义正确

### 三级降级

```
第一级：原生 JSON Schema（strict=True）  —— 可靠性最高，模型要求最高
第二级：JSON Mode（json_object）          —— 只保证可解析，不保证 Schema
第三级：纯 Prompt + 正则提取（无 schema）  —— 兼容所有模型，可靠性最低
```

**为何降级而非「只用最高级」**：不同模型对结构化输出的支持差异很大。三级降级让结构化输出在廉价模型（fast）上也能工作，只是在必要时才走更低级。代价是每级失败多一次模型调用，加上错误回喂（[问题 3](../../../issues/integration/llm/2026-08-08-degrade-instead-of-error-reask.md)）每级最多 2 次回喂，三级全失败最多 7 次调用（token 消耗）。

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
   ├─ 第一级：_build_json_schema_request(schema)  →  _try_extract（strict JSON Schema）
   │        失败（不支持 / 解析失败）↓
   ├─ 第二级：_build_json_mode_request()           →  _try_extract（JSON mode）
   │        失败 ↓
   └─ 第三级：_fallback_extract（prompt + 正则，无 response_format）
        全失败 → 返回 None
   │
   └─ 每级内部：llm_service.generate(...)
         ├─ RetryHandlerManager（重试/熔断）
         ├─ ReservationLimiterManager（限流 reserve/settle）
         └─ StreamParser.parse_non_stream（解析完整响应）
```

**分层**（2026-08-12 架构调整：纯工具函数提取为模块级私有函数，`StructuredOutput` 只保留业务编排与边界决策）：

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 统一入口 | `LLMService.generate_structured` | 对外唯一入口，委托 extract |
| 编排（类内） | `StructuredOutput.extract` | 三级降级顺序控制（成功即返回，失败逐级下探） |
| 提取流程（类内） | `StructuredOutput._try_extract` / `_fallback_extract` | 调 generate + 解析 content + 返回 dict/None |
| 调用辅助（类内） | `_call_generate` | 统一调 generate + 下游异常分类（NON_RETRYABLE raise / 可恢复返回 None） |
| 短路辅助（类内） | `_raise_boundary` | 统一 refusal / tool_calls / truncated 短路抛异常（truncated 为主调用点可选） |
| 分类（类内） | `_classify_result` | 四态分类（ok / truncated / refusal / tool_calls） |
| 请求构造（模块级） | `_build_json_schema_request` / `_build_json_mode_request` | 构造 response_format 参数 |
| schema 处理（模块级） | `_enforce_no_extra_fields` | 深拷贝递归补全 `additionalProperties:false` |
| 解析校验（模块级） | `_try_parse_json` / `_parse_and_validate` / `_collect_schema_errors` / `_validate_schema` | JSON 解析 + Schema 校验（含错误收集） |
| 消息构造（模块级） | `_build_reask_messages` | 构造错误回喂消息（clone + assistant 失败输出 + user 反馈） |

> **职责划分**：模块级函数为**纯工具**（无类状态、可独立测试）；`StructuredOutput` 保留**业务编排与边界决策**（三级降级顺序、截断/拒答/工具调用短路）。`register_config` / `_default_max_tokens` 保留类内——需访问类属性，属类自身状态。

---

## 组件详解

### _build_json_schema_request — 原生 JSON Schema 请求

```python
def _build_json_schema_request(schema: dict[str, Any]) -> dict[str, Any]:
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
- **schema 补全后透传**：`extract` 深拷贝递归补 `additionalProperties:false`（[问题 4](../../../issues/integration/llm/2026-08-08-extra-fields-not-rejected.md)），再传给服务商。调用方仍负责字段设计（少而明确、用枚举）
- **模块级私有函数**（2026-08-12 架构调整）：纯工具函数提取出类，`extract` 内部直接调用

### _build_json_mode_request — JSON mode 请求

```python
def _build_json_mode_request() -> dict[str, str]:
    """构建普通 JSON mode 请求参数（无 Schema 约束）。"""
    return {"type": "json_object"}
```

**要点**：

- 只要求输出可解析 JSON，**不含 Schema 约束**——字段/类型/枚举不做保证
- 对应工业级「JSON mode 是基础能力，不要当成 Schema 保证」

### extract — 三级降级编排

```python
@staticmethod
async def extract(llm_service, messages, schema, model_key="fast", max_tokens=None):
    """三级降级：JSON Schema → JSON Mode → 正则提取。
    完整实现见 structured.py::StructuredOutput.extract。"""
    schema = _enforce_no_extra_fields(schema)   # 递归补 additionalProperties:false（[问题 4](../../../issues/integration/llm/2026-08-08-extra-fields-not-rejected.md)）
    if max_tokens is None:
        max_tokens = StructuredOutput._default_max_tokens   # 注入的默认预算

    # 第一级：原生 JSON Schema（strict）→ 成功即返回
    try:
        result = await StructuredOutput._try_extract(
            llm_service, messages, _build_json_schema_request(schema),
            model_key, schema=schema, max_tokens=max_tokens,
        )
    except StructuredTruncationError:
        return None   # 截断短路，不降级
    if result is not None:
        return result

    # 第二级：JSON mode（同第一级，response_format 换 _build_json_mode_request()）
    ...  # 截断短路返回 None，成功即返回

    # 第三级：正则提取（无 response_format）
    try:
        return await StructuredOutput._fallback_extract(
            llm_service, messages, model_key, schema=schema, max_tokens=max_tokens,
        )
    except StructuredTruncationError:
        return None   # 截断短路
```

**要点**：

- **顺序固定**：高约束 → 低约束，每级成功即返回，不再下探
- **级间判定是「解析成功 + Schema 校验通过」**：`_try_extract` 返回非 None dict 且通过校验才视为成功（见 [问题 1](../../../issues/integration/llm/2026-08-08-no-schema-validation.md)，已修复）
- **截断短路返回 None，拒答向上抛**：截断与降级正交、拒答需业务层差异化处理（[问题 2](../../../issues/integration/llm/2026-08-08-finish-reason-refusal-unchecked.md)）
- **schema 补全**：入口递归补 `additionalProperties:false` 拒绝额外字段（[问题 4](../../../issues/integration/llm/2026-08-08-extra-fields-not-rejected.md)）
- **透明**：调用方不知道命中了哪级（除拒答外，正常返回 None 仅表示「三级耗尽」）
- **模块级函数调用**（2026-08-12）：`_enforce_no_extra_fields` / `_build_json_schema_request` / `_build_json_mode_request` 为模块级私有函数，`extract` 直接调用

### _try_extract — 单级提取（response_format 形态）

```python
@staticmethod
async def _try_extract(llm_service, messages, response_format, model_key, schema=None, max_tokens=None):
    """单级提取（response_format 形态）。完整实现见 structured.py::StructuredOutput._try_extract。"""
    result = await StructuredOutput._call_generate(   # 统一下游异常分类（NON_RETRYABLE raise / 可恢复返回 None）
        llm_service, messages, model_key, max_tokens, response_format=response_format,
    )
    if result is None:
        return None   # 下游失败 → 降级

    failure = StructuredOutput._classify_result(result)   # 解析前三态检查
    if failure == "truncated":
        retry = await StructuredOutput._call_generate(   # 截断：扩 max_tokens 重试 1 次
            llm_service, messages, model_key,
            max_tokens * 2 if max_tokens is not None else StructuredOutput._default_max_tokens * 2,
            response_format=response_format, stage="结构化输出截断重试",
        )
        if retry is None:
            return None
        StructuredOutput._raise_boundary(   # 重试后 truncated/refusal/tool_calls 一律短路
            StructuredOutput._classify_result(retry), retry, "截断重试后")
        result = retry
    else:
        StructuredOutput._raise_boundary(failure, result, "结构化输出")   # refusal/tool_calls 短路

    # 正常：解析 + 校验（错误回喂 _REASK_MAX_RETRIES 次）
    content = result.content
    for _ in range(_REASK_MAX_RETRIES):
        parsed, errors = _parse_and_validate(content, schema)
        if parsed is not None:
            return parsed
        retry = await StructuredOutput._call_generate(
            llm_service, _build_reask_messages(messages, content, "\n".join(errors)),
            model_key, max_tokens, response_format=response_format, stage="结构化输出回喂",
        )
        if retry is None:
            return None
        StructuredOutput._raise_boundary(   # 回喂内 refusal/tool_calls/truncated 一律短路
            StructuredOutput._classify_result(retry), retry, "回喂重试后")
        content = retry.content
    # 终态解析：最后一次回喂请求的输出尚未被解析（循环「解析→失败→再请求」
    # 以请求收尾）——补一次解析避免「最后一次修正成功被静默丢弃 + 白付一次调用」
    parsed, _ = _parse_and_validate(content, schema)
    return parsed  # 回喂耗尽（含终态）仍失败 → None 触发降级
```

**要点**：

- **temperature=0**：结构化输出要确定性，禁用采样随机
- **`_call_generate` 统一调用**（2026-08-12 重构）：主调用 / 截断重试 / 回喂三处调 generate 统一走 `_call_generate`——内部处理下游异常分类（NON_RETRYABLE raise、可恢复返回 None），`stage` 参数提供日志前缀，消除重复的 try/except
- **`_raise_boundary` 统一短路**（2026-08-12 重构）：refusal / tool_calls / truncated 三类短路统一走 `_raise_boundary`——截断重试后 / 回喂循环内 / fallback 均可用；**主调用点截断除外**（需扩 token 重试而非短路，用 `else` 让主路径 `_raise_boundary` 只在未走截断分支时执行）
- **解析前三态检查**：`_classify_result` 区分截断/拒答/正常（[问题 2](../../../issues/integration/llm/2026-08-08-finish-reason-refusal-unchecked.md)）——截断扩 token 重试 1 次、拒答短路，均不进入降级链
- **错误回喂**：解析/校验失败回喂错误重试 `_REASK_MAX_RETRIES=2` 次，耗尽返回 None 触发降级（[问题 3](../../../issues/integration/llm/2026-08-08-degrade-instead-of-error-reask.md)）。**终态解析**（2026-08-16）：循环退出补一次解析——最后一次回喂请求的输出（循环「解析→失败→再请求」以请求收尾）必须被解析，否则模型在最后一次修正成功的结果被静默丢弃 + 白付一次调用
- **下游失败降级（B3，2026-08-09）**：`generate` 对**可恢复错误**（超时/5xx/429）重试耗尽返回 None → 降级到下一级；对**不可恢复错误**（4xx/认证/熔断开启）抛异常 → structured 记录 ERROR 日志后 re-raise，不再白打降级请求。与截断/拒答短路区分（审核修复）
- **response_format 400 降级（2026-08-16）**：`_call_generate` 识别「明确因 response_format 不被支持而 400」（`_is_unsupported_response_format_error`：状态码 400 + 错误信息含 response_format/json_schema）→ 记 WARNING 后返回 None 触发降级，而非当致命错误上抛——兑现「模型不支持 strict JSON Schema 时降级到 JSON Mode」的降级链契约（如 DeepSeek 等不支持 `json_schema` 类型的兼容网关）；**其余 NON_RETRYABLE 400 仍上抛**（调用方 bug 不静默吞掉）
- **回喂内截断一律短路**：不与扩 token 逻辑组合，防 token 爆炸（审核修复，对齐顶层「截断与降级正交」）

### _fallback_extract — 正则兜底提取（无 response_format）

```python
@staticmethod
async def _fallback_extract(llm_service, messages, model_key, schema=None, max_tokens=None):
    """纯 prompt 约束降级方案（边界检查 + 正则定位 JSON 块）。
    完整实现见 structured.py::StructuredOutput._fallback_extract。"""
    result = await StructuredOutput._call_generate(
        llm_service, messages, model_key, max_tokens, stage="结构化输出 fallback",
    )
    if result is None:
        return None
    StructuredOutput._raise_boundary(   # 截断/拒答/工具调用一律短路（第三级无降级可走）
        StructuredOutput._classify_result(result), result, "结构化输出（fallback）")

    # 提取 JSON：剥 Markdown 代码块 → 整体解析 → 正则定位 {..} 块（prose 包裹场景）
    content = result.content.strip()
    fenced = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
    fenced = re.sub(r"\s*```$", "", fenced, flags=re.MULTILINE)
    parsed = _try_parse_json(fenced, schema)
    if parsed is not None:
        return parsed
    m = re.search(r"\{.*\}", fenced, flags=re.DOTALL)
    if m:
        parsed = _try_parse_json(m.group(0), schema)
        if parsed is not None:
            return parsed
    return None
```

**要点**：

- **无 response_format**：走纯 prompt 约束（prompt 由调用方构建），模型可能输出解释/代码块
- **`_call_generate` + `_raise_boundary`**（2026-08-12 重构）：调用与短路统一走辅助方法——下游异常分类、truncated/refusal/tool_calls 短路均与 `_try_extract` 一致
- **`_try_parse_json` 模块级**（2026-08-12 架构调整）：渐进提取的 JSON 解析 + 校验为模块级私有函数，与 `_parse_and_validate` / `_collect_schema_errors` / `_validate_schema` 同类
- **渐进提取**：先剥 Markdown 代码块整体解析，失败后**正则定位首个 `{` 到末个 `}`** 提取候选块（审核修复——模型在 JSON 前后加说明文字也能救回）
- **不修事实**：只做语法级归一化（剥代码块/定位块），不猜测意图、不补字段、不映射枚举——符合工业级「JSON repair 只能修语法，不能修事实」
- **截断/拒答短路**：第三级到头无降级可走，截断不扩 token（纯 prompt 约束重试收益不定），拒答/截断抛异常（[问题 2](../../../issues/integration/llm/2026-08-08-finish-reason-refusal-unchecked.md)）
- **每级多次模型调用**：三级全失败最多 7 次调用（strict 1+回喂 2 + JSON mode 1+回喂 2 + 正则 1，token 消耗，见「已知边界」边界 4）

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
         │     └─ 解析失败 / 非 dict / 可恢复失败(None) → 降级；不可恢复异常 → 向上抛
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
- **全失败返回 None**：调用方需自行处理 None（降级 / 追问 / 返回 unknown，见「工业级对照」）。**例外**：拒答抛 `StructuredRefusalError`、工具调用抛 `StructuredToolCallError`、下游不可恢复错误 re-raise（均非 None，B3）——调用方需捕获差异化处理（安全兜底 / 工具循环 / 修复参数）

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

**注意**：结构化降级的每一级都会经过完整的重试/熔断/限流——三级全失败意味着最多 7 次模型调用（strict 1+回喂 2 + JSON mode 1+回喂 2 + 正则 1，每组含内部重试）。这是「降级保证兼容 + 错误回喂」的代价（见「已知边界」边界 4）。

---

## 配置项清单（隐含参数）

structured.py 的调用参数（W4，2026-08-09 参数化）：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `temperature` | `0` | 结构化输出确定性（禁采样随机） |
| `max_tokens` | `StructuredOutput.register_config()` 注入（Container 注入 `settings.llm_structured_max_tokens`，默认 2048） | 输出预算；调用方经 `generate_structured(max_tokens=...)` 覆盖；截断时扩 2 倍重试 1 次 |
| `model_key` | `fast`（可传参覆盖） | 默认用廉价快速模型，必要时传 reasoning/main |
| `response_format` | 级内构造 | 第一级 json_schema / 第二级 json_object / 第三级无 |

> **与限流的关系**：结构化模块不直接接触限流配置（RPM/TPM 由 generate 内部按 model_key 读取），但其每次调用都按 `model_key` 扣配额——`max_tokens` 参数会直接影响 `_count_prompt_tokens` 的 TPM 预留量（调用方传更大预算，限流预留随之增大），见 [limiter.md](limiter.md)。

---

## 已知边界与设计取舍

1. **成功判定 = 可解析 dict + Schema 校验通过**：`_try_extract` / `_fallback_extract` 在 `json.loads` + `isinstance(dict)` 后，经 `_validate_schema`（jsonschema）校验字段类型/枚举/必填/范围，失败记日志并返回 `None` 触发降级。已修复（2026-08-08，见 [问题 1](../../../issues/integration/llm/2026-08-08-no-schema-validation.md)）。**残留边界**：校验失败先回喂重试再降级（问题 3 已覆盖），回喂仍失败才降级。
2. **finish_reason / refusal 已检查**：`_classify_result` 解析前四态分类（截断/拒答/工具调用/正常），截断扩 token 重试 1 次、拒答抛 `StructuredRefusalError`、工具调用抛 `StructuredToolCallError`（均不进降级链），记区分日志。已修复（2026-08-08 问题 2 + 2026-08-09 审核补充 tool_calls）。**残留边界**：截断扩 token 重试仅 1 次，超限后放弃（返回 None，不降级）；拒答抛 `StructuredRefusalError`、工具调用抛 `StructuredToolCallError`，调用方需捕获并差异化处理（安全兜底 / 按工具调用走 Agent 循环）。
3. **错误感知重试已实现**：`_try_extract` 校验失败先回喂错误重试（`_REASK_MAX_RETRIES=2`），耗尽才降级；strict/JSON mode 级回喂，正则级不加。已修复（2026-08-08，见 [问题 3](../../../issues/integration/llm/2026-08-08-degrade-instead-of-error-reask.md)）。**残留边界**：回喂重试增加模型调用次数（最坏 7 次/请求），token 消耗放大。
4. **多级降级 + 回喂 = 多次模型调用**：三级全失败最多 7 次调用（strict 1+回喂 2 + JSON mode 1+回喂 2 + 正则 1），token 消耗放大。这是「兼容所有模型 + 错误感知重试」的显式代价——换取廉价模型可用性与纠错能力，而非默认接受解析失败。
5. **输出预算可配置（W4 + 2026-08-10 注入化）**：`max_tokens` 由 `StructuredOutput.register_config()` 注入（Container 读 `settings.llm_structured_max_tokens`，默认 2048），调用方经 `generate_structured(max_tokens=...)` 按业务覆盖；截断时扩 2 倍重试 1 次（随参数缩放，不再硬编码 4096），超限后放弃。
6. **额外字段默认拒绝**：`extract` 对 schema 深拷贝并递归补全 `additionalProperties:false`（问题 4 已修复），模型无法扩展接口。显式 `additionalProperties:true` 仍被尊重。
7. **校验失败日志脱敏（2026-08-16，见 [LLM-037](../../../issues/integration/llm/2026-08-16-schema-validation-log-redaction.md)）**：`_validate_schema` 失败日志用**结构化字段摘要**（字段路径 + `validator` + `validator_value`）替代 `e.message`——jsonschema 的 `message` 会嵌入完整实例值（如 `'<超长值>' is too long`），模型输出可能含业务敏感数据（Yield RCA 场景为良率/晶圆数据），全量落盘是泄露面；`parsed` 经 `_truncate_json_for_log` 截断到 `_LOG_TRUNCATE_LIMIT`（500 字符）安全长度，`schema`（接口契约，非业务数据）保留完整。可观测性不因脱敏而丢失（校验器名/字段路径仍在）。**回喂循环日志同源修复**：`_collect_schema_errors` 保留 `e.message` 供**回喂模型**（模型需要具体错误修正），新增 `_collect_schema_error_summaries`（结构化字段摘要）用于 `_try_extract` 回喂日志——回喂模型与日志落盘两套文本，敏感数据不因日志泄露、模型纠错能力不损。

---

## 问题记录

> 结构化模块审核发现的问题（问题 → 修复 → 工业级对照）已提取归档，完整生命周期（发现 → 分析 → 修复 → 验证 → 教训）见：

- [问题 1：解析后无 Schema 校验](../../../issues/integration/llm/2026-08-08-no-schema-validation.md)
- [问题 2：不检查 finish_reason / refusal](../../../issues/integration/llm/2026-08-08-finish-reason-refusal-unchecked.md)
- [问题 3：降级而非错误感知重试](../../../issues/integration/llm/2026-08-08-degrade-instead-of-error-reask.md)
- [问题 4：额外字段不拒绝](../../../issues/integration/llm/2026-08-08-extra-fields-not-rejected.md)

### 已覆盖的工业级实践

| 工业级要求 | 本项目状态 |
| --- | --- |
| 统一组件封装，不散落 `json.loads` | ✅ `generate_structured()` 统一入口 |
| 能用 Structured Outputs 不用 JSON mode | ✅ 第一级 `json_schema(strict=True)` |
| JSON mode 不做 Schema 保证 | ✅ 定位为第二级降级 |
| repair 只修语法不修事实 | ✅ 第三级仅剥 Markdown 代码块，未引入 json-repair 库 |
| API 边界失败（超时/429/5xx） | ✅ 可靠性层（retry/限流/熔断）透明覆盖 |
| **模型不支持 response_format（400）** | ✅ `_call_generate` 识别「response_format 不被支持的 400」→ 降级到 JSON mode（2026-08-16） |
| 审计日志 | ✅ `llm_call` 业务事件（请求/用量/耗时，generate 内部） |
| 模型输出当不可信输入 | ✅ 三级降级 + 重试 + 熔断的整体设计意图 |
| **程序校验（Schema）** | ✅ `_validate_schema`（jsonschema）本地校验，三级降级全覆盖（[问题 1](../../../issues/integration/llm/2026-08-08-no-schema-validation.md)） |
| **API 边界检查（refusal/截断）** | ✅ `_classify_result` 三态分类，截断扩 token 重试 1 次、拒答短路（[问题 2](../../../issues/integration/llm/2026-08-08-finish-reason-refusal-unchecked.md)） |
| **错误感知重试** | ✅ `_try_extract` 回喂循环（`_REASK_MAX_RETRIES=2`），strict/JSON mode 级回喂，校验失败先回喂再降级（[问题 3](../../../issues/integration/llm/2026-08-08-degrade-instead-of-error-reask.md)） |
| **禁额外字段** | ✅ `_enforce_no_extra_fields` 递归补全 `additionalProperties:false`，默认拒绝额外字段（[问题 4](../../../issues/integration/llm/2026-08-08-extra-fields-not-rejected.md)） |

> **超出本模块范围**（属于 Agent 层与上层业务，structured 模块不负责）：语义/业务正确、工具执行权在后端、权限/幂等键、评测集闭环、SFT。这些由 Agent 循环与业务规则承载。

### 速查表

| 我们的缺陷 | 工业级正确做法 | 我们的现状 | 参照实现 | 结论 |
| --- | --- | --- | --- | --- |
| 解析后无 Schema 校验 | 程序校验是必需品（本地 jsonschema/Pydantic 校验） | ✅ `_validate_schema`（jsonschema）三级全覆盖，校验失败先回喂再降级 | zhuwei.fun 生产级方案 · jsonschema 库 | ✅ 已修复（2026-08-08） |
| 不检查 finish_reason/refusal | API 边界检查：截断扩 token 重试、拒答走安全兜底 | ✅ `_classify_result` 三态分类：截断扩 token 重试 1 次、拒答短路（均不进降级链），记区分日志 | OpenAI 文档 · zhuwei.fun 失败处理表 | ✅ 已修复（2026-08-08） |
| 降级而非错误感知重试 | 带具体校验错误回喂模型，`max_retries=2~3` 后进降级路径 | ✅ `_try_extract` 回喂循环（`_REASK_MAX_RETRIES=2`），strict/JSON mode 级回喂，耗尽才降级 | zhuwei.fun 错误感知 retry | ✅ 已修复（2026-08-08） |
| 额外字段不拒绝 | `additionalProperties:false` + Pydantic `extra="forbid"` | ✅ `_enforce_no_extra_fields` 递归补全，默认拒绝额外字段；显式 `true` 尊重 | JSON Schema 规范 | ✅ 已修复（2026-08-08） |
| 多级降级 = 多次调用 | 降级路径是兜底不是默认路径 | 三级全失败最多 7 次调用（strict 1+回喂 2 + JSON mode 1+回喂 2 + 正则 1） | — | ✅ 设计取舍，文档记录 |
| 吞异常无区分 | 失败类型可观测、分别处理 | 截断/拒答/回喂均区分日志（问题 2/3）；纯解析失败仍静默降级 | zhuwei.fun 失败分类表 | ⚠️ 低，解析失败细分日志非必要 |

---

## 相关文档

- [llm.md](llm.md)（LLM 层总览，含「结构化输出：三级降级 vs 单一方式」对比）
- [retry.md](retry.md)（可靠性层：重试/熔断，generate 内部对结构化调用透明生效）
- [limiter.md](limiter.md)（限流层：reserve/settle，按 model_key 扣配额）
- [streaming.md](streaming.md)（响应解析：`parse_non_stream` 供 generate 使用）