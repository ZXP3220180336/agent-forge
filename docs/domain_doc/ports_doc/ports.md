# 领域端口契约对外接口文档

> **对应代码**：`app/domain/ports/`
> **更新日期**：2026-08-29
> **文档定位**：领域端口契约模块——领域层拥有的 6 个抽象契约（依赖倒置），由集成层 / 应用层结构实现、装配根注入；服务对象为领域层 Agent / 推理策略（调用方）与集成层实现方
> **实现状态**：✅ 已实现（6 端口全部落地，随实现方测试覆盖）

---

## 📋 目录

- [领域端口契约对外接口文档](#领域端口契约对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [对外接口](#对外接口)
    - [`LLMGateway` / `StreamResult`](#llmgateway--streamresult)
    - [`ToolGateway` / `ToolResult` / `ErrorCode`](#toolgateway--toolresult--errorcode)
    - [`ContextBudgetPort`](#contextbudgetport)
    - [`CostLimiterPort`](#costlimiterport)
    - [`EmbeddingPort`](#embeddingport)
    - [`TokenCounter`](#tokencounter)
  - [内部实现组织](#内部实现组织)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

领域端口契约是领域层**拥有的抽象契约**（依赖倒置）——领域层 Agent / 推理策略只依赖这些 Protocol，不感知集成层实现。6 个端口覆盖领域层对外的全部依赖面：LLM 调用（含成本估算）、工具执行、上下文预算、成本上限、文本向量化、Token 计量。

### 模块结构

```text
app/domain/ports/
├── __init__.py          # 子包导出（6 端口 + 结果载体）
├── llm_gateway.py       # LLMGateway / StreamResult —— LLM 调用 + 成本估算契约
├── tool_gateway.py      # ToolGateway / ToolResult / ErrorCode —— 工具执行契约
├── context_budget.py    # ContextBudgetPort —— 上下文预算管理（横切）
├── cost_limiter.py      # CostLimiterPort —— 成本上限护栏（横切）
├── embedding_port.py    # EmbeddingPort —— 文本向量化
└── token_counter.py     # TokenCounter —— Token 计量
```

### 设计原则

1. **依赖倒置**：领域层定义抽象契约，集成层 / 应用层结构实现，装配根 `container.py` 注入
2. **结构子类型**：`Protocol + runtime_checkable`，实现方不强制继承，只需满足签名
3. **契约中立**：端口定义零外部框架依赖，只引用 `app.shared.types`（`Messages` / `SessionId` 等）
4. **横切能力入端口**：上下文预算（`ContextBudgetPort`）与成本上限（`CostLimiterPort`）是领域层横切护栏，统一归应用层（`context_manager` / `cost_limiter`）实现，所有 Agent 模式共享
5. **应用层依赖领域端口**：应用层组件（如 `CostLimiter`）经领域端口（`LLMGateway.calculate_cost`）接入集成能力，不直接 import 集成层——对齐 `ContextManager` 经 `TokenCounter` 端口先例

### 依赖关系

```text
领域层 Agent / 推理策略 / 应用层 ContextManager / CostLimiter
        ▼ 依赖（依赖倒置）
ports/（抽象契约，本模块）
        ▲ 结构实现（依赖倒置）
app/integration/   LLMGateway→LLMService · ToolGateway→ToolService
                   EmbeddingPort→EmbeddingService · TokenCounter→TiktokenTokenCounter
app/application/   ContextBudgetPort→ContextManager · CostLimiterPort→CostLimiter
```

---

## 对外接口

> 端口无单一 Facade——6 个 Protocol 各自是领域层的独立依赖面。契约 = 方法签名 + 返回 + 语义，全部与当前 `.py` 代码一致。

### `LLMGateway` / `StreamResult`

**文件**：[llm_gateway.py](../../../app/domain/ports/llm_gateway.py)

LLM 调用契约（流式 / 非流式 / 结构化 / 成本估算），实现方 `LLMService`。

| 方法 | 签名 | 返回 | 说明 |
| --- | --- | --- | --- |
| `async_generate` | `(messages, tools=None, temperature=0.2, max_tokens=4096, result=None, model_key="main", cancel_event=None)` | `AsyncGenerator[str]` | 流式生成：yield reasoning / message SSE 事件，增量结果写入 `result` |
| `generate` | `(messages, tools=None, temperature=0, max_tokens=1024, response_format=None, model_key="fast")` | `StreamResult \| None` | 非流式生成（简单任务） |
| `generate_structured` | `(messages, schema, model_key="fast", max_tokens=None)` | `dict \| None` | 结构化输出（非 Agent 提取场景） |
| `calculate_cost` | `(usage, model="")` | `dict[str, float]` | 成本估算（LLM 能力）：按 model 定价折算 usage 为 `{cost_usd, input_cost, output_cost}`（round 6）；供成本上限护栏经同一端口接入 |

**`StreamResult`（单轮结果载体）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `content` / `reasoning_content` | `str` | 回答 / 思考内容 |
| `has_reasoning` | `bool` | 是否出现 reasoning_content 字段（含空串）——区分「未返回」与「返回空」，供编排层决策回喂 |
| `finish_reason` | `str \| None` | 终止原因（stop / length / tool_calls） |
| `tool_calls` | `list[dict]` | LLM 请求的工具调用 |
| `usage` | `dict \| None` | token 用量明细 |
| `refusal` | `str \| None` | 拒答内容 |
| `error` | `str \| None` | 调用失败原因（create 失败 / 流中断放弃 / 取消）；None=成功。正常空回不置位 |

### `ToolGateway` / `ToolResult` / `ErrorCode`

**文件**：[tool_gateway.py](../../../app/domain/ports/tool_gateway.py)

工具执行契约，实现方 `ToolService`。

| 方法 | 签名 | 返回 | 说明 |
| --- | --- | --- | --- |
| `get_openai_tools` | `()` | `list[dict[str, Any]]` | 导出全部工具为 OpenAI tool schema（供 LLM 调用注入） |
| `execute` | `(name, parameters, timeout=None, max_retries=None, retry_delay=1.0)` | `ToolResult` | 执行单个工具（参数为 dict 或 JSON 字符串） |

**`ToolResult`（执行结果载体）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `success` | `bool` | 是否成功 |
| `content` / `error` | `str` | 成功内容 / 失败原因（LLM 归因） |
| `error_code` | `ErrorCode \| None` | 系统级失败分类（工具业务错误为 None） |
| `metadata` | `dict \| None` | 扩展元数据 |
| `execution_time` | `float \| None` | 执行耗时（秒） |
| `retry_count` | `int` | 重试次数 |

**`ErrorCode`（系统级错误码，StrEnum）**：`NOT_REGISTERED`（未注册）/ `JSON_PARSE`（参数解析失败）/ `VALIDATION`（参数校验失败）/ `REJECTED`（审批拒绝）/ `TIMEOUT`（执行超时）/ `UNKNOWN`（未捕获异常）。

### `ContextBudgetPort`

**文件**：[context_budget.py](../../../app/domain/ports/context_budget.py)

Agent 运行中上下文预算管理（横切护栏），实现方 `ContextManager`。

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `trim_messages` | `(messages, *, max_rounds, max_tokens) -> None` | 就地裁剪 messages 到预算内：保留 system/user 前缀 + 最近 N 轮 assistant/tool 配对（轮次 + token 双层护栏）；None=不限 |

### `CostLimiterPort`

**文件**：[cost_limiter.py](../../../app/domain/ports/cost_limiter.py)

Agent 运行中成本上限护栏（横切），实现方 `CostLimiter`（应用层，经 `LLMGateway.calculate_cost` 取成本估算——不直接依赖集成层）。每轮 usage 累加后折算成本，超限走 `COST_EXCEEDED` 错误分发（默认 STOP 停机）。无状态纯函数——实现可安全共享为单例，并发请求不串扰。

| 方法 | 签名 | 返回 | 说明 |
| --- | --- | --- | --- |
| `check` | `(usage)` | `tuple[bool, float]` | 判定**跨轮累计** usage 折算成本是否超限，返回 `(exceeded, cost_usd)`；cost 供停机错误文案与观测 |

### `EmbeddingPort`

**文件**：[embedding_port.py](../../../app/domain/ports/embedding_port.py)

文本向量化契约，实现方 `EmbeddingService`。

| 方法 | 签名 | 返回 | 说明 |
| --- | --- | --- | --- |
| `embed` | `(text, model=None)` | `list[float]` | 单文本向量化 |
| `embed_batch` | `(texts, model=None)` | `list[list[float]]` | 批量向量化（自动分批，保持输入顺序） |

### `TokenCounter`

**文件**：[token_counter.py](../../../app/domain/ports/token_counter.py)

Token 计量契约，实现方 `TiktokenTokenCounter`（tiktoken）。

| 方法 | 签名 | 返回 | 说明 |
| --- | --- | --- | --- |
| `count_tokens` | `(text)` | `int` | 单段文本 token 数 |
| `count_messages_tokens` | `(messages)` | `int` | messages 列表总 token（含格式开销与末尾回复开销） |

---

## 内部实现组织

| 端口 | 文件 | 实现方 |
| --- | --- | --- |
| `LLMGateway` / `StreamResult` | [llm_gateway.py](../../../app/domain/ports/llm_gateway.py) | [LLMService](../../integration_doc/llm_doc/llm.md) |
| `ToolGateway` / `ToolResult` / `ErrorCode` | [tool_gateway.py](../../../app/domain/ports/tool_gateway.py) | [ToolService](../../integration_doc/tools_doc/tools.md) |
| `ContextBudgetPort` | [context_budget.py](../../../app/domain/ports/context_budget.py) | [ContextManager](../../application_doc/context_doc/context.md) |
| `CostLimiterPort` | [cost_limiter.py](../../../app/domain/ports/cost_limiter.py) | [CostLimiter](../../application_doc/context_doc/context.md) |
| `EmbeddingPort` | [embedding_port.py](../../../app/domain/ports/embedding_port.py) | [EmbeddingService](../../integration_doc/embedding_doc/embedding.md) |
| `TokenCounter` | [token_counter.py](../../../app/domain/ports/token_counter.py) | [TiktokenTokenCounter](../../integration_doc/llm_doc/token_counter.md) |

实现方文档为各层模块文档，端口契约的调用语义见本文件，实现细节见实现方文档。

---

## 相关文档

- [领域层说明](../README.md)（主文档）
- [Agent 模块对外接口文档](../agent_doc/agent.md)（端口主要调用方）
- [推理策略模块](../reasoning_doc/reasoning.md)（端口调用方）
- [集成层说明](../../integration_doc/README.md)（实现方所在层）
- [应用层说明](../../application_doc/README.md)（ContextManager 实现方所在层）
- [共享类型](../../shared_doc/types.md)（`Messages` / `SessionId` / `UserId`）
