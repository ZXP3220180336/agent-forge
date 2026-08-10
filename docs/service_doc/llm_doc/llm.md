# LLM 层说明文档

## 📋 目录

- [LLM 层说明文档](#llm-层说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [快速开始](#快速开始)
    - [基础用法](#基础用法)
    - [流式生成](#流式生成)
    - [非流式生成（简单任务）](#非流式生成简单任务)
    - [结构化输出](#结构化输出)
    - [向量化](#向量化)
    - [成本计算](#成本计算)
  - [架构设计](#架构设计)
    - [调用流程](#调用流程)
    - [数据流](#数据流)
  - [核心模块详解](#核心模块详解)
    - [ClientManager — 连接池管理](#clientmanager--连接池管理)
    - [RetryHandler — 重试与熔断](#retryhandler--重试与熔断)
    - [StreamParser — 流式解析](#streamparser--流式解析)
    - [StructuredOutput — 结构化输出](#structuredoutput--结构化输出)
    - [LLM 调用业务事件（llm\_call）](#llm-调用业务事件llm_call)
    - [限流：acquire 与 reserve/settle 双形态](#限流acquire-与-reservesettle-双形态)
    - [CostTracker — 成本计算](#costtracker--成本计算)
    - [EmbeddingService — 向量化](#embeddingservice--向量化)
  - [设计选型对比](#设计选型对比)
    - [连接管理：ClientManager vs 每次新建](#连接管理clientmanager-vs-每次新建)
    - [重试策略：指数退避 + 抖动 vs 固定间隔](#重试策略指数退避--抖动-vs-固定间隔)
    - [熔断：CircuitBreaker vs 无熔断](#熔断circuitbreaker-vs-无熔断)
    - [限流算法：Token Bucket vs 漏桶 vs 队列](#限流算法token-bucket-vs-漏桶-vs-队列)
    - [结构化输出：三级降级 vs 单一方式](#结构化输出三级降级-vs-单一方式)
    - [流式解析：纯函数 vs 有状态类](#流式解析纯函数-vs-有状态类)
    - [日志：JSON 结构化 vs 文本](#日志json-结构化-vs-文本)
  - [配置参考](#配置参考)
    - [模型配置](#模型配置)
    - [嵌入配置](#嵌入配置)
    - [高级配置](#高级配置)
  - [最佳实践](#最佳实践)
    - [1. 模型选择原则](#1-模型选择原则)
    - [2. 结构化输出](#2-结构化输出)
    - [3. 流式 vs 非流式](#3-流式-vs-非流式)
    - [4. 成本控制](#4-成本控制)
    - [5. 异常处理](#5-异常处理)
    - [6. 日志利用](#6-日志利用)
  - [常见问题](#常见问题)
    - [Q: `LLMService` 和 `ClientManager` 的关系是什么？](#q-llmservice-和-clientmanager-的关系是什么)
    - [Q: 什么时候用 `async_generate`，什么时候用 `generate`？](#q-什么时候用-async_generate什么时候用-generate)
    - [Q: CircuitBreaker 开了怎么恢复？](#q-circuitbreaker-开了怎么恢复)
    - [Q: 如何切换不同模型（GPT-4 → DeepSeek）？](#q-如何切换不同模型gpt-4--deepseek)
    - [Q: 结构化的三级降级什么时候会触发？](#q-结构化的三级降级什么时候会触发)
    - [Q: 如何增加一个新的模型种类（如超高速模型）？](#q-如何增加一个新的模型种类如超高速模型)
    - [Q: 如何接入非 OpenAI 兼容的 API？](#q-如何接入非-openai-兼容的-api)
    - [Q: 调用 `generate_structured` 报 TypeError？](#q-调用-generate_structured-报-typeerror)
  - [设计决策记录](#设计决策记录)
    - [流式整流重试](#流式整流重试)
    - [配额缺口：重试/降级不计入限流申请](#配额缺口重试降级不计入限流申请)
  - [当前进度与遗留](#当前进度与遗留)
    - [已实现](#已实现)
    - [遗留未定事项](#遗留未定事项)
    - [下一步计划](#下一步计划)

---

## 模块概述

### 核心功能

LLM 层是系统的**模型通信基础设施**，负责所有与大语言模型的交互：

- **连接管理**：全局共享 AsyncOpenAI 连接池，支持 main / reasoning / fast 三种模型按需切换
- **重试与熔断**：指数退避 + 随机抖动 + CircuitBreaker 熔断器 + fallback 降级链
- **流式解析**：逐 chunk 解析流式响应，提取 reasoning / message / tool_calls / usage
- **非流式通道**：适合简单任务的低延迟通道（分类、提取、标签）
- **结构化输出**：三级降级策略（JSON Schema → JSON Mode → 正则提取）
- **请求日志**：每次 LLM 调用的元数据记录，JSON 格式输出
- **客户端限流**：双 Token Bucket 限流（RPM + TPM）
- **成本计算**：按模型用量估算费用
- **向量化**：文本嵌入（Embedding），支持批量与缓存

### 模块结构

```
app/services/
├── llm_service.py                 ← 统一 Facade（外部模块入口）
├── embedding_service.py           ← 文本向量化服务
├── llm/                           ← LLM 内部实现子包
│   ├── __init__.py                ← 包入口，导出所有子模块
│   ├── client.py                  ← ClientManager 连接池管理
│   ├── retry.py                   ← RetryHandler + CircuitBreaker
│   ├── streaming.py               ← StreamParser 流式/非流式解析
│   ├── structured.py              ← StructuredOutput 结构化输出
│   ├── reservation_limiter.py     ← ReservationLimiter 客户端限流（reserve/settle + 自适应预留，生产唯一）
│   └── cost_tracker.py            ← CostTracker 成本计算
```

### 设计原则

1. **Facade 模式**：`LLMService` 是唯一的外部入口，`llm/` 子包的内部组件不对外暴露
2. **纯化职责**：每个模块只做一件事 —— `StreamParser` 只解析 chunk，不构造事件
3. **三权分立**：逻辑拆分遵循以下边界：

| 层次     | 职责                   | 对应模块                                                      |
| -------- | ---------------------- | ------------------------------------------------------------- |
| 传输层   | 连接、代理、认证       | `ClientManager`                                               |
| 可靠性层 | 重试、熔断、限流、降级 | `RetryHandler`, `ReservationLimiter`, `CircuitBreaker`        |
| 数据层   | 流式/非流式解析        | `StreamParser`, `StructuredOutput`                            |
| 治理层   | 日志、成本             | `log_event("llm_call")`, `CostTracker`                        |
| 服务层   | 统一对外接口           | `LLMService`（Facade）                                        |

> 限流双实现：`rate_limiter.py`（acquire 形态，独立保留）与 `reservation_limiter.py`（reserve/settle 形态，llm_service 实际使用）。合并文档见 [limiter.md](limiter.md)。

### 依赖关系

```
外部调用方（Agent / Chat Router 等）
        │
        ▼
  LLMService（Facade）
    ├── ClientManager ──────────→ AsyncOpenAI(OpenAI SDK)
    ├── RetryHandler
    │     ├── RetryConfig
    │     └── CircuitBreaker
    ├── StreamParser
    ├── ReservationLimiter ───────→ reserve/settle + 自适应预留（Fenic 式）
    └── CostTracker

  EmbeddingService ─── AsyncOpenAI(GET /embeddings)
```

---

## 快速开始

### 基础用法

```python
from app.services import LLMService

# 方式一：自动使用 ClientManager（需先注册）
llm = LLMService()
result = llm.async_generate(
    messages=[{"role": "user", "content": "你好"}],
)

# 方式二：手动传入参数
llm = LLMService(api_key="sk-xxx", model="gpt-4")
```

### 流式生成

```python
sr = StreamResult()
async for event in llm.async_generate(messages, result=sr):
    # event 是 SSE 事件字符串，可直接发送给前端
    print(event)

print(sr.content)       # 完整回复
print(sr.usage)         # Token 用量
```

### 非流式生成（简单任务）

```python
result = await llm.generate(
    messages=[{"role": "user", "content": "分类：今天天气很好"}],
    temperature=0,
)
print(result.content)   # → "正面"
```

### 结构化输出

```python
schema = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
    },
    "required": ["name", "age"],
    # 默认补全：extract 会自动补 additionalProperties:false（拒绝额外字段混入）。
    # 显式写出更清晰，也方便阅读者理解「模型不能扩展接口」的契约。
    "additionalProperties": False,
}

data = await llm.generate_structured(
    messages=[{"role": "user", "content": "张三，28岁"}],
    schema=schema,
)
# → {"name": "张三", "age": 28}
```

### 向量化

```python
from app.app_state import app_state

vector = await app_state.embedding_service.embed("你好世界")
# → [0.012, -0.034, ...] 共 1536 维
```

### 成本计算

```python
cost = LLMService.calculate_cost(
    usage={"prompt_tokens": 500, "completion_tokens": 200},
    model="gpt-4",
)
# → {"cost_usd": 0.027, "input_cost": 0.015, "output_cost": 0.012}
```

---

## 架构设计

### 调用流程

```
                     start
                       │
                       ▼
  ┌───────── ClientManager.get_client() ─────────┐
  │   1. 查缓存 → 有则返回                       │
  │   2. 无缓存 → 创建 AsyncOpenAI + 缓存        │
  │   3. 可选 HTTP 代理                           │
  └─────────────────┬────────────────────────────┘
                     │
                     ▼
  ┌───────── ReservationLimiter.reserve() ────────┐
  │   1. RPM 桶预留（固定 1）                     │
  │   2. TPM 桶预留（估算 token，自适应时高分位） │
  │   3. 配额不足则阻塞等待（锁外 sleep）          │
  │   4. 请求后 settle() 退差 / cancel() 全额退    │
  └─────────────────┬────────────────────────────┘
                     │
                     ▼
  ┌───────── RetryHandler.execute() ──────────────┐
  │   1. CircuitBreaker.allow_request()           │
  │   2. 重试循环（指数退避 + 抖动）              │
  │   3. 全部失败 → fallback_fn（降级模型）       │
  │   4. 成功 → CircuitBreaker.record_success()   │
  └─────────────────┬────────────────────────────┘
                     │
                     ▼
  ┌───────── StreamParser ────────────────────────┐
  │   逐 chunk 解析：                              │
  │   - reasoning_token → build_reasoning_event() │
  │   - message_token   → build_message_event()   │
  │   - tool_call_deltas → accumulate → merge     │
  │   - usage           → StreamResult.usage      │
  └─────────────────┬────────────────────────────┘
                     │
                     ▼
  ┌────── log_event_async("llm_call") ────────────┐
  │   记录 model, tokens, duration, success       │
  └─────────────────┬────────────────────────────┘
                     │
                     ▼
                    end
```

### 数据流

```
  请求流                  响应流（流式）
  ┌─────┐               ┌─────────────────┐
  │用户输入│ → messages  │ chunk 1: reasoning│ → yield reasoning_event
  └─────┘               │ chunk 2: message  │ → yield message_event
                         │ chunk 3: tool_call│ → accumulate delta
                         │ chunk 4: message  │ → yield message_event
                         │ chunk N: usage    │ → StreamResult.usage
                         └─────────────────┘

  响应流（非流式）
  ┌─────────────────┐
  │ response 完整对象 │ → StreamParser.parse_non_stream() → StreamResult
  └─────────────────┘
```

---

## 核心模块详解

### ClientManager — 连接池管理

**文件**：`app/services/llm/client.py`

#### 功能

1. 全局共享 `AsyncOpenAI` client 实例，避免每次请求重复创建连接和 SSL 握手
2. 支持三种预配置 client：`main` / `reasoning` / `fast`，按需获取
3. 支持 HTTP 代理

#### 实现方式

```python
class ClientManager:
    _instances: dict[str, AsyncOpenAI] = {}   # client 缓存
    _configs: dict[str, dict] = {}             # 配置存储

    @classmethod
    def register_config(cls, key, api_key, base_url, model, **extra):
        """注册配置（不立即创建 client）"""

    @classmethod
    def get_client(cls, key="main") -> AsyncOpenAI:
        """懒加载：首次调用时创建，后续复用"""
        if key not in cls._instances:
            cls._instances[key] = AsyncOpenAI(**cfg)
        return cls._instances[key]
```

#### 为什么选择「懒加载 + 全局缓存」

| 维度     | 当前做法                     | 替代方案           |
| -------- | ---------------------------- | ------------------ |
| 创建时机 | 第一次 `get_client` 时懒加载 | 初始化时全部创建   |
| 复用粒度 | 按 key 缓存实例              | 每次请求创建新实例 |

**选择理由**：

- 懒加载避免冷启动时创建不需要的 client（如果只用到 main，reasoning 就不需要初始化）
- 全局缓存复用 TCP 连接，减少握手开销 —— 一个 AsyncOpenAI 实例内部维护连接池
- 按 key 隔离不同模型的配置，但共享连接池资源

#### 代理支持

通过 `httpx.AsyncClient(proxy=...)` 实现，只在配置了 `proxy_url` 时启用：

```python
def _build_proxied_client(proxy_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=proxy_url)
```

---

### RetryHandler — 重试与熔断

**文件**：`app/services/llm/retry.py`

#### 功能

1. **指数退避**：`base_delay × 2^attempt`，上限 `max_delay`
2. **随机抖动**：在退避基础上 `random.uniform(0, delay)`，防止羊群效应
3. **CircuitBreaker**：滑动窗口错误率判定熔断（参考 Hystrix），超时后半开探测
4. **Fallback 降级**：主模型全部失败后尝试备用模型
5. **错误分类**：区分可重试 / 不可恢复 / 限流 / 熔断触发

#### 错误分类策略

```python
class ErrorCategory(Enum):
    RETRYABLE      # 超时、5xx → 可重试
    NON_RETRYABLE  # 4xx、未知异常 → 直接抛出（不重试）
    RATE_LIMITED   # 429 → 可重试但退避，不计入熔断
```

| 异常类型                           | 分类          | 处理方式              |
| ---------------------------------- | ------------- | --------------------- |
| `TimeoutError` / `APITimeoutError` | RETRYABLE     | 重试                  |
| 5xx                                | RETRYABLE     | 重试                  |
| 429 `RateLimitError`               | RATE_LIMITED  | 重试 + 尊重 Retry-After，**不计入熔断** |
| 401 / 403 / 422                    | NON_RETRYABLE | 直接抛出              |
| 400                                | NON_RETRYABLE | 直接抛出              |

#### CircuitBreaker 状态机

```
    ┌──────────┐    failure >= threshold     ┌──────────┐
    │  CLOSED  │ ──────────────────────────► │   OPEN   │
    │  (正常)  │                              │  (熔断)  │
    └────┬─────┘                              └────┬─────┘
         ▲                                         │
         │          recovery_timeout 超时            │
         │    ┌─────────────────────────┐          │
         │    │                         │          │
         │    ▼                         │          │
         │  ┌──────────┐               │          │
         │  │HALF_OPEN │ ◄─────────────┘          │
         │  │ (半开)   │    允许多少探针            │
         │  └────┬─────┘                           │
         │       │                                 │
         └───────┘                                 │
         成功恢复        ───────────────────────────┘
                        失败则继续熔断
```

#### Fallback 降级链

```
call_fn（主模型）→ 重试 3 次 → 全部失败
    → fallback_fn（备选模型）→ 成功 → 半开恢复
    → fallback_fn 也失败 → 抛出最后一次异常
```

#### 为什么选择「CircuitBreaker + 指数退避 + 抖动」

| 特性     | 当前做法                | 替代方案                   |
| -------- | ----------------------- | -------------------------- |
| 退避算法 | 指数 × 2，加随机抖动    | 固定间隔、线性增加、无抖动 |
| 熔断     | 滑动窗口错误率 + 探针   | 无熔断 / 连续失败计数      |
| 降级     | 一个 fallback 函数      | 无降级 / 多级降级链        |

**选择理由**：

- **指数退避**是 API 调用的标准做法 —— 快失败、慢重试，给服务端恢复时间
- **随机抖动**防止多个请求在同一个时刻重试（羊群效应），这在多个 Agent 并发时尤其重要
- **CircuitBreaker** 让系统在 API 不可用时快速失败而不是空等，同时通过探针自动恢复
- **fallback 降级**保证主模型故障时系统仍可用（降级到便宜模型或备用服务商）

#### 其他可选的方法

1. **gRPC 重试策略**：Google API 的 1.5 倍指数退避 + 抖动，可以在 `grpc.retry` 中内置
2. **Resilience4j** 模式：Java 生态的重试 + 熔断模式，支持基于异常类型的复杂路由
3. **Tenacity** 库：Python 生态的通用重试库，支持自定义退避、条件重试，但缺少熔断
4. **无抖动退避**：固定间隔重试，实现简单但容易造成羊群效应

---

### StreamParser — 流式解析

**文件**：`app/services/llm/streaming.py`（设计文档见 [streaming.md](streaming.md)）

#### 功能

1. 逐 chunk 解析 OpenAI 流式响应，提取五种信息
2. 合并增量 tool_call 片段为完整对象
3. 解析非流式完整响应

#### 核心数据结构

```python
class ParsedChunk:
    reasoning_token: str | None    # 推理过程片段（如 DeepSeek-R1）
    message_token: str | None      # 回复文本片段
    finish_reason: str | None      # 停止原因（stop / length / tool_calls）
    usage: dict | None             # Token 用量（最后一个 chunk）
    tool_call_deltas: list[ToolCallDelta] | None  # 工具调用增量

class ToolCallDelta:
    index: int                     # 工具索引（多工具时区分）
    id: str                        # 工具 call ID
    function_name: str             # 函数名增量
    function_arguments: str        # 参数 JSON 增量
```

#### 解析策略

```python
@staticmethod
def parse_chunk(chunk) -> ParsedChunk:
    # 1. 无 choices → 检查 usage（最后一个 chunk）
    if not chunk.choices or not chunk.choices[0].delta:
        if chunk.usage:
            result.usage = chunk.usage.model_dump()
        return result

    # 2. 有 choices → 解析 delta
    delta = chunk.choices[0].delta
    #   - reasoning_content（推理模型）
    #   - content（回复）
    #   - tool_calls（工具调用增量）
    #   - finish_reason（停止原因）
```

#### 为什么选择「静态方法 + 纯函数」

| 维度 | 当前做法               | 替代方案                 |
| ---- | ---------------------- | ------------------------ |
| 设计 | 纯静态方法，无状态     | 有状态类，维护内部缓冲区 |
| 产出 | `ParsedChunk` 数据对象 | SSE 事件字符串           |

**选择理由**：

- **纯函数**便于测试 —— 输入一个 mock chunk，输出 `ParsedChunk`，无需 mock 内部状态
- **与事件层解耦** —— `StreamParser` 不知道 `build_message_event()` 的存在，调用方决定如何渲染
- **同时支持流式与非流式** —— `parse_non_stream()` 复用同一个数据模型

#### 其他可选的方法

1. **有状态解析器**：内部维护 `tool_call_deltas` 缓冲区，在 `finish_reason` 时自动合并。优点是调用方少一个 `merge_tool_calls` 步骤，缺点是测试时需要重置状态
2. **正则提取**：直接从原始响应字符串中提取内容，不可靠且不兼容流式
3. **回调模式**：`on_token()` / `on_tool_call()` 回调，耦合度高

---

### StructuredOutput — 结构化输出

**文件**：`app/services/llm/structured.py`（设计文档见 [structure.md](structure.md)）

> **统一入口（2026-08-07）**：对外唯一入口为 `LLMService.generate_structured()`，它委托 `StructuredOutput.extract()` 三级降级；`StructuredOutput` 是内部实现载体（接收完整 messages）。

#### 功能

根据 JSON Schema 从 LLM 输出中提取结构化数据，三级降级：

```
第一级：原生 JSON Schema（strict=True）
   ↓ 不支持或解析失败
第二级：JSON Mode（response_format="json_object"）
   ↓ 不支持或解析失败
第三级：纯 Prompt 约束 + 正则提取（无 schema）
```

#### 为什么选择「三级降级」

| 级别 | 方法                                           | 可靠性 | 模型要求           |
| ---- | ---------------------------------------------- | ------ | ------------------ |
| 1    | `response_format={"type": "json_schema", ...}` | 最高   | gpt-4o-mini 以上   |
| 2    | `response_format={"type": "json_object"}`      | 中     | gpt-3.5-turbo 以上 |
| 3    | Prompt + 正则                                  | 低     | 所有模型           |

**选择理由**：

- 不同模型对结构化输出的支持差异很大 —— gpt-4o 支持 `strict=True` 的 JSON Schema，但 deepseek-chat 可能只支持 `json_object`
- 三级降级让结构化输出在廉价模型（fast）上也能工作，只是在必要时用主模型
- 降级是透明的 —— 调用方无需知道底层用了哪种方式

#### 其他可选的方法

1. **只用原生 JSON Schema**：简单直接，但在不支持 strict 的模型上会失败
2. **只用 Prompt 约束**：兼容所有模型，但解析容易失败（返回 Markdown 代码块、多余说明等）
3. **通过工具调用（tool_use）实现**：把 JSON Schema 转为 function definition，利用模型对工具调用的高可靠性。优点是稳定性接近原生 Schema，缺点是消耗更多 token
4. **第三方解析库（如 `json-repair` / `outlines`）**：自动修复残缺 JSON，但引入外部依赖

---

### LLM 调用业务事件（llm_call）

**来源**：`app/utils/logger.py` 全局日志框架的业务事件机制（`log_event_async`）；早期 `LLMLogger`（`app/services/llm/logger.py`）已移除，职责并入全局框架。详见 [logging.md](../../logging.md)。

#### 功能

1. 记录每次 LLM 调用的元数据（模型、Token、耗时、是否成功）
2. 事件名为 `llm_call`，字段经 extra 注入进全局 JSON 结构化日志
3. 不记录敏感信息（只记录 messages 数量，不记录内容）

#### 事件字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `model` | str | 模型名 |
| `messages_count` | int | 消息条数 |
| `temperature` | float | 温度 |
| `has_tools` | bool | 是否携带工具 |
| `stream` | bool | 是否流式 |
| `success` | bool | 是否成功 |
| `error` | str\|None | 错误信息（成功为 None） |
| `duration` | float | 耗时（秒） |
| `prompt_tokens` | int\|None | 输入 Token |
| `completion_tokens` | int\|None | 输出 Token |
| `total_tokens` | int\|None | 总计 Token |
| `finish_reason` | str\|None | 停止原因 |

#### 为什么选「全局框架 + 业务事件」

| 维度     | 当前做法                                        | 替代方案           |
| -------- | ----------------------------------------------- | ------------------ |
| 格式     | 全局框架 JSON（文件）/ 人类可读（控制台）       | 纯文本、CSV        |
| 记录方式 | `log_event_async` → `asyncio.to_thread`         | 同步写入、异步队列 |
| 归属     | 全局日志框架（横切关注点，各模块统一）          | LLM 层私有         |

**选择理由**：

- **全局统一**：日志是横切关注点，所有模块（LLM/服务/Agent/API）走同一双 handler，消费端（ELK/Datadog/Graylog）无需分模块解析
- **异步写入**：`asyncio.to_thread` 不阻塞 LLM 调用主流程
- **结构化可检索**：事件名即 `message`，字段进 JSON，可按事件/字段查询

#### 输出示例（文件 JSON）

```json
{"timestamp": "2026-08-04T18:42:08+0800", "level": "INFO", "logger": "app.events", "message": "llm_call", "model": "gpt-4o", "messages_count": 5, "success": true, "total_tokens": 1234, "duration": 2.34, "finish_reason": "stop"}
```

#### 使用方式

```python
from app.utils.logger import log_event_async

event_fields = {
    "model": "gpt-4o",
    "messages_count": 5,
    "temperature": 0,
    "has_tools": False,
    "stream": True,
}
event_fields["success"] = True
event_fields["duration"] = 2.34
await log_event_async("llm_call", **event_fields)
```

---

### 限流：acquire 与 reserve/settle 双形态

**文件**：`app/services/llm/rate_limiter.py` + `app/services/llm/reservation_limiter.py`

使用双 Token Bucket 算法，同时限制 **RPM**（每分钟请求数）与 **TPM**（每分钟 Token 消耗量）。限流完整设计（算法代码、5 种参考算法可视化、等待 vs 拒绝、自适应预留、工业级对比）见 **[limiter.md](limiter.md)**。

**双形态（acquire 已移除，2026-08-10）**：

| 形态 | 文件 | 特点 | 生产使用者 |
| --- | --- | --- | --- |
| acquire（学习参考） | 已移除，代码见 limiter.md | 一次性扣减，不退款 | 无 |
| reserve/settle | `reservation_limiter.py` | 先预留、请求后 `settle(actual)` 退差 / `cancel()` 全额退；含自适应预留（`reserve_adaptive`，开关默认关） | ✅ `llm_service.py` |

---

### CostTracker — 成本计算

**文件**：`app/services/llm/cost_tracker.py`

#### 功能

1. 根据模型和 Token 用量计算预估成本
2. 支持前缀匹配（`deepseek-chat-v2` → `deepseek-chat` 定价）
3. 支持会话级成本累计

#### 定价表

```python
MODEL_PRICING = {
    "gpt-4":             {"input": 0.03,    "output": 0.06},
    "gpt-4-turbo":       {"input": 0.01,    "output": 0.03},
    "gpt-4o":            {"input": 0.0025,  "output": 0.01},
    "gpt-4o-mini":       {"input": 0.00015, "output": 0.0006},
    "deepseek-chat":     {"input": 0.0005,  "output": 0.001},
    "deepseek-reasoner": {"input": 0.0005,  "output": 0.002},
    "claude-sonnet-4":   {"input": 0.003,   "output": 0.015},
    ...
}
```

#### 为什么选择前缀匹配

```python
@staticmethod
def _find_price(model: str) -> dict:
    # 1. 精确匹配
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # 2. 前缀匹配（如 deepseek-chat-v2 → deepseek-chat）
    for key in sorted(MODEL_PRICING.keys(), key=len, reverse=True):
        if model.startswith(key):
            return MODEL_PRICING[key]
    # 3. 默认均价
    return DEFAULT_PRICE
```

**理由**：

- 模型名经常带版本号后缀（`gpt-4o-2024-08-06`、`deepseek-chat-v2`），精确匹配会 miss
- 前缀匹配按 key 长度降序，避免 `deepseek-chat` 匹配到 `deepseek-reasoner` 的定价

---

### EmbeddingService — 向量化

**文件**：`app/services/embedding_service.py`

#### 功能

1. 单文本嵌入、批量嵌入（自动分批）
2. 可选 LRU 缓存（MD5 哈希键）
3. 保持输入顺序，缓存穿透只请求未命中项

#### 缓存策略

```python
def embed_batch(self, texts):
    results = [None] * len(texts)
    uncached = []

    # 1. 查缓存
    for i, t in enumerate(texts):
        key = self._make_cache_key(t, model)
        if key in self._cache:
            results[i] = self._cache[key]
        else:
            uncached.append(i)

    # 2. 只请求未命中的
    for batch in chunk(uncached, max_batch_size):
        response = await self._client.embeddings.create(input=batch)

    return results
```

---

## 设计选型对比

### 连接管理：ClientManager vs 每次新建

| 维度       | ClientManager（当前）      | 每次新建 AsyncOpenAI     |
| ---------- | -------------------------- | ------------------------ |
| TCP 连接   | 复用（keep-alive）         | 每次新建                 |
| 资源开销   | 低（N 个 key 共享连接池）  | 高（每个请求独立连接池） |
| 配置变更   | `register_config()` 后重建 | 直接修改参数             |
| 实现复杂度 | 中等（全局字典）           | 低                       |

### 重试策略：指数退避 + 抖动 vs 固定间隔

| 维度         | 指数退避 + 抖动        | 固定间隔               | 线性增加 |
| ------------ | ---------------------- | ---------------------- | -------- |
| 服务端压力   | 低（重试越晚间隔越大） | 高（所有重试间隔相同） | 中       |
| 羊群效应防护 | 有（随机抖动）         | 无                     | 无       |
| 平均等待时间 | 短                     | 取决于固定值           | 中       |
| 适用场景     | LLM API（多并发）      | 本地服务（单请求）     | 批处理   |

### 熔断：CircuitBreaker vs 无熔断

| 维度         | CircuitBreaker（当前） | 无熔断           |
| ------------ | ---------------------- | ---------------- |
| API 不可用时 | 快速拒绝（秒级）       | 反复重试直到超时 |
| 下游保护     | 是（减少无用请求）     | 否               |
| 自动恢复     | 是（半开探针）         | 否（需手动介入） |
| 实现开销     | 几十行状态维护         | 0                |

### 限流算法：Token Bucket vs 漏桶 vs 队列

| 维度       | Token Bucket（当前） | 漏桶               | asyncio.Queue  |
| ---------- | -------------------- | ------------------ | -------------- |
| 突发处理   | 允许（桶容量积攒）   | 不允许（恒定速率） | 允许但排队     |
| 实现复杂度 | 低                   | 低                 | 低             |
| 精度       | 中（连续速率）       | 高（严格整形）     | 中（队列大小） |
| 适用场景   | 多数 API 限流        | 视频流、IoT        | 任务调度       |

### 结构化输出：三级降级 vs 单一方式

| 维度       | 三级降级（当前） | 仅 JSON Schema    | 仅 Prompt  |
| ---------- | ---------------- | ----------------- | ---------- |
| 兼容性     | 所有模型         | strict 支持的模型 | 所有模型   |
| 可靠性     | 高（自动降级）   | 最高              | 低         |
| 实现复杂度 | 中               | 低                | 最低       |
| 失败率     | 低               | 低（仅适用模型）  | 高（30%+） |

### 流式解析：纯函数 vs 有状态类

| 维度     | 纯函数（当前）                    | 有状态类                                 |
| -------- | --------------------------------- | ---------------------------------------- |
| 测试     | 直接 mock chunk，无副作用         | 需要 reset 状态                          |
| 并发安全 | 天然安全                          | 需要注意状态清理                         |
| 使用方式 | `StreamParser.parse_chunk(chunk)` | `parser.feed(chunk)` → `parser.result()` |
| 灵活性   | 高（调用方控制流程）              | 中（内部控制流程）                       |

### 日志：JSON 结构化 vs 文本

| 维度     | JSON（当前）       | 纯文本         |
| -------- | ------------------ | -------------- |
| 可解析性 | 高（自动消费）     | 低（需要正则） |
| 可读性   | 低（视觉噪音）     | 高（终端友好） |
| 查询能力 | 强（任意字段过滤） | 弱（全文搜索） |

---

## 配置参考

LLM 层所有配置项集中在 `app/config/settings.py`：

### 模型配置

| 配置项                      | 类型  | 默认值                        | 说明                    |
| --------------------------- | ----- | ----------------------------- | ----------------------- |
| `llm_api_key`               | str   | `""`                          | API 密钥                |
| `llm_base_url`              | str   | `"https://api.openai.com/v1"` | API 端点                |
| `llm_model_id`              | str   | `"gpt-4"`                     | 主模型（用于对话）      |
| `llm_temperature`           | float | `0.2`                         | 主模型温度              |
| `llm_max_tokens`            | int   | `4096`                        | 主模型最大输出          |
| `llm_reasoning_model_id`    | str   | `""`                          | 推理模型（空=用主模型） |
| `llm_reasoning_temperature` | float | `0.7`                         | 推理模型温度            |
| `llm_reasoning_max_tokens`  | int   | `8192`                        | 推理模型最大输出        |
| `llm_fast_model_id`         | str   | `""`                          | 快速模型（空=用主模型） |
| `llm_fast_temperature`      | float | `0.0`                         | 快速模型温度            |
| `llm_fast_max_tokens`       | int   | `2048`                        | 快速模型最大输出        |

### 嵌入配置

| 配置项                     | 类型 | 默认值                     | 说明     |
| -------------------------- | ---- | -------------------------- | -------- |
| `llm_embedding_model_id`   | str  | `"text-embedding-3-small"` | 嵌入模型 |
| `llm_embedding_dimensions` | int  | `1536`                     | 向量维度 |

### 高级配置

| -------------------------------------- | ----- | ------    | --------------------------------------------------- |
| `llm_max_retries`                      | int   | `2`       | 最大重试次数                                        |
| `llm_stream_max_retries`               | int   | `1`       | 流式整流重试次数（首 token 前中断才整流；`0`=禁用） |
| `llm_base_delay`                       | float | `1.0`     | 退避基数（秒）                                      |
| `llm_max_delay`                        | float | `30.0`    | 退避上限（秒）                                      |
| `llm_use_jitter`                       | bool  | `True`    | 是否启用随机抖动                                    |
| `llm_circuit_window_seconds`           | float | `10.0`    | 滑动时间窗口长度（秒）                              |
| `llm_circuit_error_threshold`          | float | `0.5`     | 窗口内错误率熔断阈值（50%）                         |
| `llm_circuit_request_volume_threshold` | int   | `20`      | 窗口内最小请求量，不足不做错误率评估                |
| `llm_circuit_all_failed_min`           | int   | `3`       | 低流量纯失败保护：全部失败且达此样本量才熔断        |
| `llm_circuit_recovery_timeout`         | float | `30.0`    | 熔断恢复超时（秒）                                  |
| `llm_circuit_half_open_max_requests`   | int   | `3`       | 半开状态最大探针数                                  |
| `llm_fallback_model_id`                | str   | `""`      | 降级备用模型                                        |
| `llm_proxy_url`                        | str   | `""`      | HTTP 代理                                           |
| `llm_main_rpm`                         | int   | `60`      | 主模型 RPM 限流                                     |
| `llm_reasoning_rpm`                    | int   | `30`      | 推理模型 RPM 限流                                   |
| `llm_fast_rpm`                         | int   | `100`     | 快速模型 RPM 限流                                   |
| `llm_main_tpm`                         | int   | `2000000` | 主模型 TPM 限流（默认参考 DeepSeek 限额）           |
| `llm_reasoning_tpm`                    | int   | `2000000` | 推理模型 TPM 限流（默认参考 DeepSeek 限额）         |
| `llm_fast_tpm`                         | int   | `2000000` | 快速模型 TPM 限流（默认参考 DeepSeek 限额）         |
| `llm_adaptive_reserve`                 | bool  | `false`   | 自适应预留开关（开启用高分位估算输出，减少占桶）     |
| `llm_reserve_quantile`                 | float | `0.95`    | 普通模型输出分位数（p95）                            |
| `llm_reserve_reasoning_quantile`       | float | `0.99`    | 推理模型分位数（p99，推理输出有相关性突发尖峰）      |
| `llm_reserve_safety_margin`            | float | `1.15`    | 安全系数（1.0~4.0，越高越保守）                      |
| `llm_reserve_min_samples`              | int   | `30`      | 冷启动阈值（样本不足回退静态上限）                   |
| `llm_reserve_window`                   | int   | `256`     | 滚动样本窗口（deque 上限）                           |

---

## 最佳实践

### 1. 模型选择原则

| 场景           | 推荐模型标识             | 原因                       |
| -------------- | ------------------------ | -------------------------- |
| 用户对话       | `main`                   | 质量最高                   |
| 深度推理       | `reasoning`              | 允许更长思考 + 更高温度    |
| 分类/提取/标签 | `fast`                   | 低延迟、低成本，确定性输出 |
| Embedding      | `llm_embedding_model_id` | 专用模型                   |

### 2. 结构化输出

```python
# 推荐：指定 fast 模型 + 明确 schema
result = await llm.generate_structured(
    messages=[system_msg, user_msg],
    schema=my_schema,
    model_key="fast",  # 结构化输出通常不需要主模型
)
```

### 3. 流式 vs 非流式

```python
# 用户交互 → 流式
async for event in llm.async_generate(messages, model_key="main"):
    yield event

# 后端处理 → 非流式
result = await llm.generate(messages, temperature=0, model_key="fast")
```

### 4. 成本控制

```python
# 每次调用后计算成本
cost = LLMService.calculate_cost(result.usage, model_name)
print(f"本次调用花费 ${cost['cost_usd']:.6f}")

# 会话级累计
session_cost = {"cost_usd": 0, "input_cost": 0, "output_cost": 0}
for each_call_usage:
    cost = CostTracker.calculate(each_call_usage, model)
    session_cost = CostTracker.accumulate(session_cost, cost)
```

### 5. 异常处理

```python
try:
    result = await llm.generate(messages)
except CircuitBreakerOpenError:
    # 熔断中，等待后重试或直接返回错误
    logger.warning("LLM 熔断中，跳过本次调用")
except Exception as e:
    logger.error(f"LLM 调用失败: {e}")
```

### 6. 日志利用

```python
# 业务事件：LLM 调用记录（全局日志框架）
from app.utils.logger import log_event_async
event_fields = {"model": "gpt-4o", "messages_count": 5, "success": True}
event_fields["duration"] = 2.34
await log_event_async("llm_call", **event_fields)
# 文件 JSON：{"message": "llm_call", "model": "gpt-4o", ...}
```

---

## 常见问题

### Q: `LLMService` 和 `ClientManager` 的关系是什么？

`ClientManager` 管理底层的 `AsyncOpenAI` 连接池，`LLMService` 调用 `ClientManager` 获取 client，并组合重试、解析、日志等能力对外暴露。`LLMService` 是 Facade，`ClientManager` 是它背后的齿轮之一。

### Q: 什么时候用 `async_generate`，什么时候用 `generate`？

- `async_generate`（流式）用于**用户可见的交互** —— 前端需要 SSE 事件流实时展示回复
- `generate`（非流式）用于**后端内部处理** —— 分类、提取、embedding，延迟更低

### Q: CircuitBreaker 开了怎么恢复？

熔断器有一个 `recovery_timeout`（默认 30 秒）。超时后进入半开状态，放行探针请求：

- 探针成功 → 熔断器关闭，恢复正常
- 探针收到 429 / 超时 / 5xx → 下游仍过载/故障，熔断器回 OPEN，重置超时计时
- 探针收到 4xx / 未知（客户端问题）→ 不改变状态、归还探针名额、异常抛给上层修复（不算健康探测，等待正常请求探测真实状态）

### Q: 如何切换不同模型（GPT-4 → DeepSeek）？

在 `.env` 或环境变量中修改配置：

```
LLM_MODEL_ID=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_REASONING_MODEL_ID=deepseek-reasoner
LLM_FAST_MODEL_ID=deepseek-chat
```

在代码中通过 `model_key` 参数切换：

```python
# 使用快速模型
result = await llm.generate(messages, model_key="fast")
# 使用推理模型
async for event in llm.async_generate(messages, model_key="reasoning"):
```

### Q: 结构化的三级降级什么时候会触发？

只有在第一级失败时才会尝试下一级：

1. **原生 JSON Schema**：模型不支持 `strict=True` 或返回了非法 JSON
2. **JSON Mode**：模型不支持 `json_object` 格式
3. **正则提取**：前两级都失败时的兜底方案

常见触发场景：使用 DeepSeek / Claude 等非 OpenAI 模型时，原生 JSON Schema 可能不支持。

### Q: 如何增加一个新的模型种类（如超高速模型）？

在 `ClientManager` 中注册新的 key，并在 `settings.py` 中添加对应配置：

```python
# settings.py 新增
llm_ultra_fast_model_id: str = ""

# app_state.py 注册
ClientManager.register_config(
    "ultra_fast",
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    model=settings.llm_ultra_fast_model_id or settings.llm_model_id,
)

# 使用
result = await llm.generate(messages, model_key="ultra_fast")
```

### Q: 如何接入非 OpenAI 兼容的 API？

如果 API 不兼容 OpenAI 的 `/chat/completions` 格式，有两种选择：

1. **通过 proxy 层转换**（推荐）：部署一个兼容层（如 LiteLLM、one-api），对外暴露 OpenAI 接口，内部转发到目标 API
2. **重新实现一个 Client 类**：继承或组合 `RetryHandler` / `StreamParser`，实现自己的传输逻辑

### Q: 调用 `generate_structured` 报 TypeError？

**A:** `StructuredOutput.extract()` 内部 `_try_extract()` 和 `_fallback_extract()` 的形参名是 `model_key`，不是 `model`。调用时须传 `model_key=model_key`：

```python
# ❌ 形参名错误
data = await llm.generate_structured(..., model="fast")

# ✅ 形参名正确
data = await llm.generate_structured(..., model_key="fast")
```

历史上曾因方法签名用 `model`、调用时用 `model_key=` 导致运行时 TypeError。

---

## 设计决策记录

> 本节收录 LLM 层的设计决策（问题 → 工业调研 → 决策 → 实现），前因后果完整记录。

### 流式整流重试

> 本节记录 `async_generate()` 流式迭代整流重试的前因后果（2026-08-01 实施）。

#### 问题背景

`retry.execute()` 只保护 `client.chat.completions.create()`（创建响应对象），真正的 `async for chunk in response:` 迭代在重试范围外。流中途断掉（读超时 / 连接重置 / 解析失败）时，旧实现只捕获报错（记日志 + 错误事件），不重试。

#### 工业调研结论（决策依据）

- **OpenAI Python SDK**：`max_retries` 只覆盖初始 HTTP 请求，不重试 mid-stream。官方维护者明确"用户已消费部分输出，mid-stream 重试语义不清晰"
- **LangChain `langchain-failover`**：只在主模型**产出第一个 token 前**死亡时 failover——"你永远不会得到重复的、半流输出"
- **awaken 运行时**：4 级恢复（ContinueText / SynthesizeToolUse / TruncateBeforeTool / WholeRestart），WholeRestart（整流重试）只在无文本、无完整工具调用时用

#### 决策（用户确认）

1. **首 token 前中断 → 整流重试**（重新 create + 重新迭代）：用户没看到任何输出，整流不会产生重复内容
2. **已产出 token 后中断 → 不整流**：避免重复输出 / token 双倍计费 / tool_calls 残缺
3. **新增独立配置 `llm_stream_max_retries`**（默认 1），不复用 `llm_max_retries`——create 重试（HTTP 请求级）与整流重试（已开始流式后重启）属不同故障阶段，需独立调优；设为 `0` 即禁用整流，行为退化为旧版
4. **整流条件复用 `classify_error`**：只有 RETRYABLE / RATE_LIMITED 的迭代异常才整流；NON_RETRYABLE（4xx/校验错误/token 截断/未知）不整流——重试大概率无效，避免白打下游
5. **create 阶段异常绝不整流**：`retry.execute` 已决定重试/熔断/fallback；400 / `CircuitBreakerOpenError` / fallback 失败走既有错误路径
6. **cancel_event 置位永不整流**：迭代内取消检查 + 整流分支退避前后各一道守卫

#### 实现要点

- `async_generate` 重构为显式 `for attempt in range(llm_stream_max_retries + 1)` 循环（async generator 无法用 `retry.execute` 直接包，因其 `call_fn` 返回 Awaitable 而非 AsyncGenerator）
- **`emitted_any` 标志**：`reasoning_token` / `message_token` / `tool_call_deltas` 任一非空即置位；`finish_reason` / `usage` 不算"首 token"（纯 usage/finish 死流仍可整流）
- **result 复用**：整流写同一 `result` 对象；`emitted_any=False` 保证 content/reasoning/tool_calls 未写，重试幂等安全。整流前清空死流的 `result.finish_reason` / `result.usage` 残留
- **退避**：复用 `llm_base_delay` / `llm_max_delay` / `llm_use_jitter`（`_stream_backoff` 辅助，公式与 create 阶段一致），不新增退避配置
- **日志**：每次尝试独立计时，失败走 `success=False` + error，成功清 `error=None`（整流成功 = 1 条失败 + 1 条成功日志）
- **fallback**：仍只绑定 create 阶段（`retry.execute` 内部），流式中断不触发额外 fallback

#### 测试

`tests/unit/test_stream_rectify.py`（21 用例）：首 token 前中断整流成功 / 已产出后不整流 / 连续中断超上限 / cancel 不整流 / create 失败不整流 / 整流尝试中 create 失败 / tool_call 增量算已产出 / 仅 usage/finish 后中断整流 / 成功路径回归 / NON_RETRYABLE 迭代异常不整流 / 日志正确性 / 限流闭环（reserve/settle/cancel）/ **熔断观察盲区（放弃喂 record_failure、整流成功不喂、NON_RETRYABLE/RATE_LIMITED/cancel 不喂）**。

#### 遗留微调（已解决）

- **熔断观察盲区 ✅ 已解决（2026-08-07）**：流式迭代「放弃时」（不整流）且异常为 RETRYABLE → 喂 `cb.record_failure()`，让熔断器感知「create 正常但流频繁中途断开」的下游故障。配套：新增 `RetryHandlerManager`（按 model_key 跨请求共享熔断器，修复熔断窗口无法跨请求积累的隐性缺陷）

---

### 配额缺口：重试/降级不计入限流申请

> 本节记录「限流申请量 vs 实际请求量」不一致的问题（2026-08-02 调研并实施）。跨模块：涉及限流（RateLimiter）与重试（RetryHandler）的配合。

#### 配额缺口：问题背景

当前集成中，`async_generate()` / `generate()` 在**进入** `retry.execute()` 前 `acquire` 一次限流（RPM 桶 1 个 + TPM 桶 estimated_tokens 个）。放行后进入重试/熔断/降级阶段——若调用失败会重试、多次失败会降级到备用模型。**这些后续真实发出的 API 请求，都发生在限流申请之后**。

用户的疑问（原文概括）：
> 我们在限流器处设定为获取 1 个请求和固定 token，限流器放行后进入重试/熔断阶段。一次调用不成功会重试，多次重试失败后降级调用——这期间产生的 token 和向底层接口请求的次数，是不是超过了向限流器申请的量？

**结论：是。** 实际请求数可能远超限流器放行的量。

#### 配额缺口：问题原因

以默认配置（`llm_max_retries=2`，retry 循环最多 3 次 call_fn）为例：

| 路径 | 是否重新 acquire | 真实请求数 |
| --- | --- | --- |
| 原始调用 | ✅ acquire 了 | 1 |
| retry 内部重试（×2） | ❌ **不 acquire** | +2 |
| fallback 降级（备用模型） | ❌ **不 acquire** | +1 |

**一次 `async_generate` 最多可发 4 次真实请求，但只申请了 1 次限流**。

具体影响：

- **RPM 桶低估**：实际放行速率 = `rpm × (1 + 重试率 + 降级率)`。下游越不稳定，放大越严重（失败率 50% 时实际请求约是 RPM 的 2 倍）。
- **TPM 桶低估**：重试时同样的 prompt 重新发送，token 消耗翻倍，但 TPM 桶只在第一次 acquire 扣过一次。
- **fallback 零限流保护**：备用模型（`llm_fallback_model_id`）有自己的配额，当前 fallback 完全不 acquire，**无任何限流**。
- **整流重试是例外**：首 token 前中断的整流每轮都重新 acquire，此路径无缺口。

#### 配额缺口：工业调研结论（决策依据）

1. **中间件链布局决定答案**：工业界把限流器和重试做成中间件链，**限流器在重试外层**——每次真实请求（含重试）都重新穿过限流器、消耗 token（如 Go 的 `tgcp`：`"Each request consumes 1 token"`，重试也计）。这是主流做法——**重试天然计入配额**。参考：[Rethinking HTTP API Rate Limiting（IEEE/arXiv）](https://ieeexplore.ieee.org/document/11366354)、[tgcp 中间件链](https://pkg.go.dev/github.com/yogirk/tgcp@v0.4.0/internal/core)
2. **IETF 留白**：RateLimit Header Fields 草案明确「规范不规定非 2xx 响应是否消耗配额」，留给服务端/客户端设计决策。无唯一正确答案，但工业实现主流倾向是**外层包裹**。[IETF 草案](https://datatracker.ietf.org/meeting/109/agenda/httpapi-drafts.pdf)
3. **重要澄清**：客户端限流的第一目的是**防突发**（别瞬间打爆服务端），不是精确记账到每一分服务端配额。服务端还有第二道闸（429 + `Retry-After`），重试请求即使客户端没 acquire，服务端可能再 429 兜底。**但当重试原因是 5xx/超时（下游故障）时，重试请求会真实打到服务端并消耗 token——客户端不 acquire 就是超额**。
4. **LLM 生态实践**：客户端 Token Bucket 限流器（如 [plsno429](https://github.com/appleparan/plsno429)）作为 **proactive** 手段在请求前限流；重试（**reactive**）走指数退避 + jitter + 尊重 `Retry-After`。两者是互补的两层，不是同一件事。参考：[OpenAI 429 官方指南](https://help.openai.com/en/articles/5955604-how-can-i-solve-429-too-many-requests-errors)

#### 配额缺口：决策（用户确认）

**acquire 移入 call_fn，但 fallback 不参与 acquire。**

- **重试计入配额**：retry.execute 内部每次重试都重新 acquire（重试=新请求，扣配额合理）。
- **fallback 不参与 acquire**：客户端限流防的是**主模型**的突发，备用模型（降级路径）无需限流保护，且独立于主模型配额。

#### 配额缺口：实现要点

- `_rate_limited_call` 闭包捕获 `limiter` / `estimated` / `client` / `kwargs`，在整流循环外定义（依赖变量均不随 attempt 变化）。
- `estimated` 在循环外计算一次（messages 在一次 `async_generate` 内不变），避免每次重试重复 tiktoken 计数。
- **整流重试**：整流循环每轮重新 `execute`，call_fn 内部再次 acquire——「新请求」语义正确。
- **代价**：重试前会先 acquire，等待与退避叠加，延迟可能增大；但语义正确（重试=新请求，扣配额合理）。

```python
async def _rate_limited_call():
    await limiter.acquire(estimated_tokens=estimated)   # 每次真实请求前都 acquire
    return await client.chat.completions.create(**kwargs)

response = await retry.execute(
    call_fn=_rate_limited_call,        # 原始 + retry 内部重试都重新 acquire
    fallback_fn=fallback_fn,           # fallback 不参与 acquire
)
```

#### 配额缺口：测试

`tests/unit/test_stream_rectify.py`：

- `test_rate_limiter_acquire_before_each_attempt`：整流 2 轮，每轮 call_fn 都 acquire（`calls == 2`）。
- `test_rate_limiter_acquire_on_retry_inside_execute`：retry.execute 内部重试也 acquire（create 第 1 次抛 RETRYABLE → 重试第 2 次，`calls == 2`）。

> **状态**：✅ 已实施（2026-08-02）。

---

## 当前进度与遗留

> 本节记录 LLM 层自身的进度、遗留工作与下一步计划（项目整体进度见 [HANDOFF.md](../../HANDOFF.md)）。

### 已实现

- 8 子模块全部落地：ClientManager / RetryHandler / StreamParser / StructuredOutput / RateLimiter / ReservationLimiter / CostTracker + `EmbeddingService`（LLM 调用日志并入全局日志框架的 `log_event_async("llm_call")` 业务事件）
- retry.py 工业级改造（2026-08-01）：滑动窗口熔断、错误分类白名单、半开探针按异常类别判定、流式迭代保护；**2026-08-05 修正**：4xx 探针不再回 OPEN，改为不改变状态 + 归还槽位 + 抛上层（详见 [retry.md](retry.md)）
- **流式整流重试（2026-08-01）**：`async_generate()` 在**产出第一个 token 前**流中断时整流重试（重新 create + 重新迭代）；已产出 token 后中断不整流。见 [设计决策记录·流式整流重试](#流式整流重试)
- **客户端限流（2026-08-02）**：`async_generate()` / `generate()` 用 `ReservationLimiterManager`（reserve/settle 形态），每次真实请求 `reserve(estimated_tokens)` 预留配额、请求后 `settle(actual)` 退差；retry 内部重试每轮重新 reserve（重试=新请求，扣配额合理），fallback 不参与 reserve
- **配额缺口闭环（2026-08-02）**：acquire 移入 call_fn，重试计入配额、fallback 不参与（见 [设计决策记录·配额缺口](#配额缺口重试降级不计入限流申请)）
- **自适应预留（2026-08-06）**：`reserve_adaptive()` + `OutputTokenEstimator`（历史实际输出的高分位 × 安全系数估算输出量，替代固定 `max_tokens` 预留），开关 `llm_adaptive_reserve` 默认关；普通模型 p95、推理模型 p99，冷启动回退静态上限，结构性解耦（provider 仍收宽裕 max_tokens 不截断，仅限流器预留下降）。详见 [limiter.md](limiter.md)「对比 3.2」。「实际消耗 > 预留」仍无法补扣（预留-结算模型结构性限制，「宁多勿少」保守取舍，已缓解未消除），详述见 [limiter.md](limiter.md)「对比 3.1·已缓解但未消除」
- **统一结构化输出入口（2026-08-07）**：`generate_structured` 委托 `StructuredOutput.extract` 三级降级（JSON Schema → JSON Mode → 正则提取），消除双入口；`extract` 签名改为接收完整 messages
- **补熔断观察盲区 + 熔断器生命周期修复（2026-08-07）**：流式迭代「放弃时」（不整流）且异常为 RETRYABLE → 喂 `cb.record_failure()`，熔断器感知「create 正常但流频繁中断」；新增 `RetryHandlerManager`（按 model_key 跨请求共享熔断器），修复熔断窗口无法跨请求积累的隐性缺陷（create 阶段熔断此前实际失效）

### 遗留未定事项

（无 —— LLM 层遗留未定事项已全部解决/决策保持/归入模块文档）

### 下一步计划

（无待办 —— LLM 层遗留未定事项已全部解决/决策保持）
