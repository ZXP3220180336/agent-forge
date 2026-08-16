# LLM 网关对外接口文档

> **对应代码**：`app/integration/llm/`
> **更新日期**：2026-08-16
> **文档定位**：LLM 模块（`app/integration/llm/`）对外接口文档——`LLMService` Facade
> 的接口契约 + 内部组件导航；服务对象为 LLM 网关的**外部调用方**（领域层 / 应用层 /
> API 层）
> **实现状态**：✅ 已实现
> **配套**：实现领域端口 `LLMGateway`（`app/domain/ports/llm_gateway.py`，领域层
> 对模型调用的唯一依赖面）；`LLMService` 为唯一对外 Facade，内部组件不对外暴露

---

## 📋 目录

- [LLM 网关对外接口文档](#llm-网关对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [对外接口](#对外接口)
    - [LLMService 方法表](#llmservice-方法表)
    - [对外异常契约](#对外异常契约)
    - [最小调用示例](#最小调用示例)
  - [内部实现组织](#内部实现组织)
  - [配置关联](#配置关联)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

LLM 模块是系统的**模型通信基础设施**，负责与大语言模型的全部交互：连接池 /
重试 / 熔断 / 限流 / 流式解析 / 整流重试 / 结构化输出 / 成本计算。对外只暴露
`LLMService` 一个 Facade（实现 `LLMGateway` 端口）。

### 模块结构

```text
app/integration/llm/
├── __init__.py                ← 包入口，导出所有子模块
├── llm_service.py             ← LLMService（唯一对外 Facade，实现 LLMGateway）
├── client.py                  ← ClientManager 连接池管理
├── retry.py                   ← RetryHandler + CircuitBreaker
├── streaming.py               ← StreamParser 流式/非流式解析
├── streaming_rectifier.py     ← StreamingRectifier 流式整流重试
├── structured.py              ← StructuredOutput 结构化输出
├── reservation_limiter.py     ← ReservationLimiter 客户端限流
└── cost_tracker.py            ← CostTracker 成本计算
```

### 设计原则

1. **Facade 模式**：`LLMService` 是唯一外部入口，`llm/` 子包内部组件不对外暴露
2. **依赖倒置**：实现领域端口 `LLMGateway`（协议），领域层 Agent 只依赖抽象，
   装配根 `container.py` 注入实现
3. **三权分立**：逻辑拆分按职责分层——传输层（连接）/ 可靠性层（重试/熔断/限流/降级）/
   数据层（解析）/ 策略层（整流）/ 治理层（日志/成本），每模块只做一件事
4. **零 settings 依赖**：各子模块配置经 `register_config()` 注入（装配根读 settings
   后统一注册），子模块不直接依赖配置

### 依赖关系

```text
外部调用方（ReActAgent 领域层 / 应用层 / API 层）
        │  依赖倒置：经 LLMGateway 端口（app/domain/ports/llm_gateway.py）
        ▼
  LLMService（Facade，实现 LLMGateway）
    ├── ClientManager ──────→ AsyncOpenAI（OpenAI 兼容 API，如 DeepSeek）
    ├── RetryHandler
    │     ├── RetryConfig
    │     └── CircuitBreaker
    ├── StreamingRectifier ──→ RectifierContext（会话共享状态）
    ├── StreamParser
    ├── StructuredOutput
    ├── ReservationLimiter ──→ reserve/settle + 自适应预留
    └── CostTracker
```

可靠性链（每次调用依次经过）：限流（reserve/settle）→ 重试/熔断/降级 →
流式整流 → 解析 → 事件日志（`llm_call`）。

---

## 对外接口

### LLMService 方法表

> 对外接口 = 被外部文件（领域层 / 应用层 / API 层 / 装配根）依赖的接口。`LLMService`
> 是 LLM 模块对外的全部依赖面；内部组件接口（`ClientManager` / `RetryHandler` 等）
> 由 Facade 内部依赖，不属对外接口，见「内部实现组织」。

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `register_config(*, fallback_model_id, adaptive_reserve, stream_max_retries)` | 同步类方法 | 注入运行期配置（装配根 `container.initialize()` 调用，零 settings 依赖） |
| `__init__(api_key="", model="", base_url="")` | 构造 | 空构造走 `ClientManager`（需先注册配置）；手动构造须 api_key / model / base_url 齐备 |
| `async_generate(messages, tools=None, temperature=0.2, max_tokens=4096, result=None, model_key="main", cancel_event=None)` | 异步生成器 | 流式生成，yield SSE 事件字符串（Agent 专用）；`cancel_event` 置位优雅终止 |
| `generate(messages, tools=None, temperature=0, max_tokens=1024, response_format=None, model_key="fast") -> StreamResult \| None` | 异步方法 | 非流式单轮生成（简单任务）；可恢复失败返回 None，不可恢复错误上抛 |
| `generate_structured(messages, schema, model_key="fast", max_tokens=None) -> dict \| None` | 异步方法 | 结构化输出三级降级（JSON Schema → JSON Mode → 正则）；拒答/工具调用抛异常 |
| `calculate_cost(usage, model="") -> dict` | 同步静态 | 按模型用量估算成本（代理 `CostTracker`） |

**返回 / 异常语义**：

- `generate`：成功返回 `StreamResult`（含 content / reasoning_content / finish_reason /
  tool_calls / usage / refusal / error）；**可恢复错误**（超时 / 5xx / 429）可靠性层重试
  耗尽返回 `None`（调用方按「业务无结果」处理）；**不可恢复错误**（4xx / 认证 / 熔断开启）
  向上抛（降级无意义，调用方需感知）
- `generate_structured`：成功返回 `dict`，三级降级耗尽返回 `None`；拒答抛
  `StructuredRefusalError`、工具调用抛 `StructuredToolCallError`（需差异化处理）
- `async_generate`：产出 SSE 事件字符串（`StreamParser` 逐 chunk 解析的增量事件）；
  失败信号透传 `StreamResult.error`（供编排层短路决策，见 [LLM-001](../../../issues/integration/llm/2026-08-16-stream-error-propagation.md)）

### 对外异常契约

> 以下异常经 `LLMService` 向上抛，外部调用方需捕获并差异化处理：

| 异常 | 触发 | 调用方处理 |
| --- | --- | --- |
| `CircuitBreakerOpenError` | 熔断开启，`generate` 拒绝主调用且无 fallback | 捕获后等待冷却 / 返回降级响应 |
| `StructuredRefusalError` | `generate_structured` 模型拒答（内容安全策略触发） | 安全兜底 / 差异化文案 |
| `StructuredToolCallError` | `generate_structured` 模型转工具调用（`finish_reason=tool_calls`） | 按工具调用走 Agent 循环 |
| `StructuredTruncationError` | 结构化输出截断（扩 token 重试后仍不完整） | 扩大预算重试 / 降级处理 |
| 不可恢复错误（原样上抛） | 4xx / 认证 / 熔断开启（`NON_RETRYABLE`） | 修复调用参数 / 返回错误响应 |

### 最小调用示例

```python
from app.container import container

llm = container.llm_service   # 装配根注入（空构造，经 ClientManager 取 client）

# 非流式生成（返回 StreamResult 或 None）
result = await llm.generate(
    messages=[{"role": "user", "content": "分类：今天天气很好"}],
    model_key="fast",
)

# 流式生成（async generator，yield SSE 事件字符串，用户交互用）
async for event in llm.async_generate(
    messages=[{"role": "user", "content": "你好"}],
    model_key="main",
):
    print(event)

# 结构化输出（三级降级，返回 dict 或 None）
schema = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}
data = await llm.generate_structured(
    messages=[{"role": "user", "content": "张三"}],
    schema=schema,
)

# 成本计算
cost = LLMService.calculate_cost(
    usage={"prompt_tokens": 500, "completion_tokens": 200},
    model="gpt-4o",
)
```

---

## 内部实现组织

> 内部 7 组件由 `LLMService` 内部依赖，不对外暴露。各组件设计文档见下表（细节不在
> 本文展开——双处维护必然漂移，Rule 1「一个事实一个家」）。

| 组件 | 文件 | 职责 | 设计文档 |
| --- | --- | --- | --- |
| `ClientManager` | client.py | 全局共享 AsyncOpenAI 连接池（main / reasoning / fast 懒加载 + 热切换关闭追踪） | [client.md](client.md) |
| `RetryHandler` + `CircuitBreaker` | retry.py | 指数退避 + 抖动 + 滑动窗口熔断 + 半开探针 + fallback 降级链 | [retry.md](retry.md) |
| `StreamParser` | streaming.py | 逐 chunk 解析流式 / 非流式响应（纯函数无状态） | [streaming.md](streaming.md) |
| `StreamingRectifier` | streaming_rectifier.py | 流式整流重试（首 token 前中断重新 create + 迭代） | [streaming_rectifier.md](streaming_rectifier.md) |
| `StructuredOutput` | structured.py | 结构化输出三级降级（JSON Schema → JSON Mode → 正则） | [structure.md](structure.md) |
| `ReservationLimiter` | reservation_limiter.py | 客户端限流（RPM + TPM 双桶，reserve/settle + 自适应预留） | [limiter.md](limiter.md) |
| `CostTracker` | cost_tracker.py | 按模型定价表估算成本（前缀匹配 + 会话级累计） | [cost_tracker.md](cost_tracker.md) |

**组件间协作**（可靠性链）：`ReservationLimiter`（事前限流）→ `RetryHandler`
（重试/熔断/降级，fallback 同 provider）→ `StreamingRectifier`（流式整流）→
`StreamParser`（解析）→ 全局日志框架 `fill_llm_event_fields("llm_call")`
（事件记录，见 [logging.md](../../utils_doc/logging.md)）。Facade 如何组织这些组件
（可靠性链 / 配额结算闭环 / 整流协作）见 [llm_service.md](llm_service.md)。

---

## 配置关联

- LLM 模块全部配置项集中在 `app/config/settings.py`（`.env` 覆盖），装配根
  `container.initialize()` 读 settings 后经各组件 `register_config()` 注入（零 settings 依赖）
- 配置明细见各组件子文档「配置项清单」：
  - 重试 / 熔断 → [retry.md](retry.md)
  - 限流（RPM / TPM + 自适应预留）→ [limiter.md](limiter.md)
  - 流式整流 → [streaming_rectifier.md](streaming_rectifier.md)
  - 结构化输出 → [structure.md](structure.md)
- 完整配置说明见 [config 文档](../../config_doc/config.md)

---

## 相关文档

- [集成层说明](../README.md)（层总览：LLM 网关在集成层中的位置）
- 组件子文档：client / retry / streaming / streaming_rectifier / structure / limiter /
  cost_tracker（见「内部实现组织」）
- [架构设计](../../architecture.md)（分层与演进路径）
- [全局日志框架](../../utils_doc/logging.md)（`llm_call` 业务事件）
- 设计决策归档：[ADR](../../../adr/integration/llm/README.md)（LLM-ADR-001~011）
- 问题记录归档：[issues](../../../issues/integration/llm/README.md)（LLM-001~037）
