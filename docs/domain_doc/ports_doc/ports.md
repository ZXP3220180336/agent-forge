# 领域端口契约对外接口文档

> **对应代码**：`app/domain/ports/`
> **更新日期**：2026-09-13
> **文档定位**：领域端口契约模块——领域层拥有的 5 个抽象契约（依赖倒置），由集成层 / 应用层结构实现、装配根注入；服务对象为领域层 Agent / 推理策略（调用方）与集成层实现方
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。

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
  - [内部实现组织](#内部实现组织)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

领域端口契约是领域层**拥有的抽象契约**（依赖倒置）——领域层 Agent / 推理策略只依赖这些 Protocol，不感知集成层实现。端口覆盖 LLM、工具、上下文预算、成本上限和文本向量化；`tool_execution.py` 另提供工具调用身份、控制信号与事实入口的领域值契约。

### 模块结构

```text
app/domain/ports/
├── __init__.py          # 子包导出（5 端口 + 结果载体）
├── llm_gateway.py       # LLMGateway / StreamResult —— LLM 调用 + 成本估算 + Token 计量契约
├── tool_gateway.py      # ToolGateway / ToolResult / ErrorCode —— 工具执行契约
├── tool_execution.py    # ToolCallContext / ToolFact / ToolFactSink —— 工具生命周期契约
├── context_budget.py    # ContextBudgetPort —— 上下文预算管理（横切）
├── cost_limiter.py      # CostLimiterPort —— 成本上限护栏（横切）
└── embedding_port.py    # EmbeddingPort —— 文本向量化
```

### 设计原则

1. **依赖倒置**：领域层定义抽象契约，集成层 / 应用层结构实现，装配根 `container.py` 注入
2. **结构子类型**：`Protocol + runtime_checkable`，实现方不强制继承，只需满足签名
3. **契约中立**：端口定义零外部框架依赖，只引用 `app.shared.types`（`Messages` / `SessionId` 等）
4. **横切能力入端口**：上下文预算（`ContextBudgetPort`）与成本上限（`CostLimiterPort`）是领域层横切护栏，统一归应用层（`context_manager` / `cost_limiter`）实现，所有 Agent 模式共享
5. **应用层依赖领域端口**：应用层组件（如 `CostLimiter` / `ContextManager`）经领域端口（`LLMGateway.calculate_cost` / `LLMGateway.count_*`）接入 LLM 能力，不直接 import 集成层——LLM 模块对外只暴露 `LLMService` Facade

### 依赖关系

```text
领域层 Agent / 推理策略 / 应用层 ContextManager / CostLimiter
        ▼ 依赖（依赖倒置）
ports/（抽象契约，本模块）
        ▲ 结构实现（依赖倒置）
app/integration/   LLMGateway→LLMService · ToolGateway→ToolService
                   EmbeddingPort→EmbeddingService
app/application/   ContextBudgetPort→ContextManager · CostLimiterPort→CostLimiter
```

---

## 对外接口

> 端口无单一 Facade——5 个 Protocol 各自是领域层的独立依赖面。契约 = 方法签名 + 返回 + 语义，全部与当前 `.py` 代码一致。

### `LLMGateway` / `StreamResult`

**文件**：[llm_gateway.py](../../../app/domain/ports/llm_gateway.py)

LLM 调用契约（流式 / 非流式 / 结构化 / 成本估算 / Token 计量），实现方 `LLMService`。

| 方法 | 签名 | 返回 | 说明 |
| --- | --- | --- | --- |
| `async_generate` | `(messages, tools=None, temperature=0.2, max_tokens=4096, result=None, model_key="main", cancel_event=None, deadline=None)` | `AsyncGenerator[str]` | 流式生成：正常增量与 provider 失败通过 SSE；业务取消完成资源收尾后抛 `LLMCancelledError`，不生成取消 SSE；deadline 抛 `LLMDeadlineExceededError`，外部 task 硬取消保留 `CancelledError`；终止前事实留在 `result` |
| `generate` | `(messages, tools=None, temperature=0, max_tokens=1024, response_format=None, model_key="fast", cancel_event=None, deadline=None)` | `StreamResult \| None` | 非流式生成；取消/期限约束真实请求等待并以 shared 类型化异常终止 |
| `generate_structured` | `(messages, schema, model_key="fast", max_tokens=None, usage=None, cancel_event=None, deadline=None)` | `dict \| None` | 结构化输出（非 Agent 提取场景）；`usage` 可变引用回填**全程累计** token 用量（含降级/截断重试/回喂的所有成功调用，供成本计量）；`cancel_event` 置位 / `deadline`（monotonic 绝对）到期 → 返回 None（不再发起后续子调用） |
| `calculate_cost` | `(usage, model="")` | `dict[str, float]` | 成本估算（LLM 能力）：按 model 定价折算 usage 为 `{cost_usd, input_cost, output_cost}`（round 6）；供成本上限护栏经同一端口接入 |
| `count_tokens` | `(text)` | `int` | 单段文本 token 数（主模型编码） |
| `count_messages_tokens` | `(messages)` | `int` | messages 总 token 数（每条消息 +4 格式开销 + content + name 额外 +1；末尾 +2）——Token 计量归属 LLM 能力，供 ContextManager 等经同一端口接入 |

**`StreamResult`（单轮结果载体）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `content` / `reasoning_content` | `str` | 回答 / 思考内容 |
| `has_reasoning` | `bool` | 是否出现 reasoning_content 字段（含空串）——区分「未返回」与「返回空」，供编排层决策回喂 |
| `finish_reason` | `str \| None` | 终止原因（stop / length / tool_calls） |
| `tool_calls` | `list[dict]` | LLM 请求的工具调用 |
| `usage` | `dict \| None` | token 用量明细 |
| `refusal` | `str \| None` | 拒答内容 |
| `error` | `str \| None` | provider 调用失败原因（create 失败 / 流中断放弃）；业务取消与 deadline 由类型化异常表达，不写本字段。正常空回不置位 |

### `ToolGateway` / `ToolResult` / `ErrorCode`

**文件**：[tool_gateway.py](../../../app/domain/ports/tool_gateway.py)

工具执行契约，实现方 `ToolService`。

| 方法 | 签名 | 返回 | 说明 |
| --- | --- | --- | --- |
| `get_openai_tools` | `()` | `list[dict[str, Any]]` | 导出全部工具为 OpenAI tool schema（供 LLM 调用注入） |
| `execute` | `(name, parameters, timeout=None, max_retries=None, retry_delay=1.0, *, call, facts)` | `ToolResult` | 执行单个工具；`call` 和 `facts` 必填且仅关键字传入，不静默创建无归属运行 |

**`ToolResult`（执行结果载体）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `success` | `bool` | 是否成功 |
| `content` / `error` | `str` | 成功内容 / 失败原因（LLM 归因） |
| `error_code` | `ErrorCode \| None` | 系统级失败分类（工具业务错误为 None） |
| `metadata` | `dict \| None` | 扩展元数据 |
| `execution_time` | `float \| None` | 执行耗时（秒） |
| `retry_count` | `int` | 重试次数 |
| `effect_state` | `ToolEffectState` | 外部效果确定性；默认 `UNKNOWN`，可信只读适配器可声明 `NONE` |

**`ErrorCode`（系统级错误码，StrEnum）**：`NOT_REGISTERED`（未注册）/ `JSON_PARSE`（参数解析失败）/ `VALIDATION`（参数校验失败）/ `REJECTED`（审批拒绝）/ `CAPACITY_EXCEEDED`（共享准入容量或等待队列已满，工具未执行）/ `TIMEOUT`（执行超时）/ `UNKNOWN`（未捕获异常）。

### 工具运行上下文与事实

**文件**：[tool_execution.py](../../../app/domain/ports/tool_execution.py)

- `ToolCallContext` 冻结 run/batch/call/operation 身份，携父级与本运行取消事件、同运行 `run_stop`、业务 deadline 和 cleanup deadline。四个身份字段必须非空，期限采用当前进程 monotonic 时刻。
- `ToolFact` 以 revision 表达一次 operation/attempt 的事实演进，分别记录执行、效果和清理状态；其 `ToolResult` 与 metadata 在构造时复制，不暴露工具后处理仍可修改的引用。
- `ToolFactSink.record()` 是同步无 I/O 入口。Integration 先持有自己的事实副本，再通知 Domain 收集器；入口异常按编程错误传播并关闭所属 run 的新业务准入。
- `ToolCancelledError`、`ToolDeadlineExceededError`、`ToolRunStoppedError` 定义在 shared，不反向依赖 Domain；局部工具 timeout 继续返回 `ToolResult(ErrorCode.TIMEOUT)`。

### `ContextBudgetPort`

**文件**：[context_budget.py](../../../app/domain/ports/context_budget.py)

Agent 运行中上下文预算管理（横切护栏），实现方 `ContextManager`。

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `count_tokens` | `(text) -> int` | 提供统一语义预算计量；字段取舍仍由具体策略决定 |
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

---

## 内部实现组织

| 端口 | 文件 | 实现方 |
| --- | --- | --- |
| `LLMGateway` / `StreamResult` | [llm_gateway.py](../../../app/domain/ports/llm_gateway.py) | [LLMService](../../integration_doc/llm_doc/llm.md)（含成本估算 / Token 计量） |
| `ToolGateway` / `ToolResult` / `ErrorCode` | [tool_gateway.py](../../../app/domain/ports/tool_gateway.py) | [ToolService](../../integration_doc/tools_doc/tools.md) |
| `ContextBudgetPort` | [context_budget.py](../../../app/domain/ports/context_budget.py) | [ContextManager](../../application_doc/context_doc/context.md) |
| `CostLimiterPort` | [cost_limiter.py](../../../app/domain/ports/cost_limiter.py) | [CostLimiter](../../application_doc/context_doc/context.md) |
| `EmbeddingPort` | [embedding_port.py](../../../app/domain/ports/embedding_port.py) | [EmbeddingService](../../integration_doc/embedding_doc/embedding.md) |

实现方文档为各层模块文档，端口契约的调用语义见本文件，实现细节见实现方文档。

---

## 相关文档

- [领域层说明](../README.md)（主文档）
- [Agent 模块对外接口文档](../agent_doc/agent.md)（端口主要调用方）
- [推理策略模块](../reasoning_doc/reasoning.md)（端口调用方）
- [集成层说明](../../integration_doc/README.md)（实现方所在层）
- [应用层说明](../../application_doc/README.md)（ContextManager 实现方所在层）
- [共享类型](../../shared_doc/types.md)（`Messages` / `SessionId` / `UserId`）
