# LLM 层说明文档

## 📋 目录

- [模块概述](#模块概述)
- [架构设计](#架构设计)
- [快速开始](#快速开始)
- [核心模块详解](#核心模块详解)
  - [ClientManager — 连接池管理](#clientmanager--连接池管理)
  - [RetryHandler — 重试与熔断](#retryhandler--重试与熔断)
  - [StreamParser — 流式解析](#streamparser--流式解析)
  - [StructuredOutput — 结构化输出](#structuredoutput--结构化输出)
  - [LLMLogger — 请求日志](#llmlogger--请求日志)
  - [RateLimiter — 客户端限流](#ratelimiter--客户端限流)
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
- [最佳实践](#最佳实践)
- [常见问题](#常见问题)

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
│   ├── logger.py                  ← LLMLogger 请求日志
│   ├── rate_limiter.py            ← RateLimiter 客户端限流
│   └── cost_tracker.py            ← CostTracker 成本计算
```

### 设计原则

1. **Facade 模式**：`LLMService` 是唯一的外部入口，`llm/` 子包的内部组件不对外暴露
2. **纯化职责**：每个模块只做一件事 —— `StreamParser` 只解析 chunk，不构造事件
3. **三权分立**：逻辑拆分遵循以下边界：

| 层次     | 职责                   | 对应模块                                        |
| -------- | ---------------------- | ----------------------------------------------- |
| 传输层   | 连接、代理、认证       | `ClientManager`                                 |
| 可靠性层 | 重试、熔断、限流、降级 | `RetryHandler`, `RateLimiter`, `CircuitBreaker` |
| 数据层   | 流式/非流式解析        | `StreamParser`, `StructuredOutput`              |
| 治理层   | 日志、成本             | `LLMLogger`, `CostTracker`                      |
| 服务层   | 统一对外接口           | `LLMService`（Facade）                          |

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
    ├── RateLimiter
    ├── LLMLogger
    └── CostTracker

  EmbeddingService ─── AsyncOpenAI(GET /embeddings)
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
  ┌───────── RateLimiter.acquire() ───────────────┐
  │   1. RPM Token Bucket 查询                    │
  │   2. TPM Token Bucket 查询                    │
  │   3. 配额不足则阻塞等待                       │
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
  ┌───────── LLMLogger.log_call() ────────────────┐
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
3. **CircuitBreaker**：连续失败 N 次后熔断，超时后半开探测
4. **Fallback 降级**：主模型全部失败后尝试备用模型
5. **错误分类**：区分可重试 / 不可恢复 / 限流 / 熔断触发

#### 错误分类策略

```python
class ErrorCategory(Enum):
    RETRYABLE        # 超时、5xx → 可重试
    CIRCUIT_TRIGGER  # 连续超时/5xx → 触发熔断
    NON_RETRYABLE    # 401、403、422 → 直接抛出
    RATE_LIMITED     # 429 → 可重试 + 标记熔断
```

| 异常类型                           | 分类          | 处理方式              |
| ---------------------------------- | ------------- | --------------------- |
| `TimeoutError` / `APITimeoutError` | RETRYABLE     | 重试                  |
| 5xx                                | RETRYABLE     | 重试                  |
| 429 `RateLimitError`               | RATE_LIMITED  | 重试 + 记入熔断计数器 |
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
| 熔断     | 基于连续失败次数 + 探针 | 无熔断 / 基于错误率        |
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

**文件**：`app/services/llm/streaming.py`

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

**文件**：`app/services/llm/structured.py`

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
- 三级降级让 `StructuredOutput.extract()` 在廉价模型（fast）上也能工作，只是在必要时用主模型
- 降级是透明的 —— 调用方无需知道底层用了哪种方式

#### 其他可选的方法

1. **只用原生 JSON Schema**：简单直接，但在不支持 strict 的模型上会失败
2. **只用 Prompt 约束**：兼容所有模型，但解析容易失败（返回 Markdown 代码块、多余说明等）
3. **通过工具调用（tool_use）实现**：把 JSON Schema 转为 function definition，利用模型对工具调用的高可靠性。优点是稳定性接近原生 Schema，缺点是消耗更多 token
4. **第三方解析库（如 `json-repair` / `outlines`）**：自动修复残缺 JSON，但引入外部依赖

---

### LLMLogger — 请求日志

**文件**：`app/services/llm/logger.py`

#### 功能

1. 记录每次 LLM 调用的元数据（模型、Token、耗时、是否成功）
2. 输出 JSON 格式的结构化日志
3. 不记录敏感信息（只记录 messages 数量，不记录内容）

#### 记录字段

```python
@dataclass
class LLMRequestRecord:
    timestamp: float            # 调用时间
    model: str                  # 模型名
    request_id: str             # 请求追踪 ID
    messages_count: int         # 消息条数
    prompt_tokens: int | None   # 输入 Token
    completion_tokens: int | None  # 输出 Token
    total_tokens: int | None    # 总计 Token
    temperature: float          # 温度
    has_tools: bool             # 是否携带工具
    stream: bool                # 是否流式
    duration: float             # 耗时（秒）
    success: bool               # 是否成功
    error: str | None           # 错误信息
    finish_reason: str | None   # 停止原因
```

#### 为什么选择「JSON 结构化日志」

| 维度     | 当前做法                          | 替代方案           |
| -------- | --------------------------------- | ------------------ |
| 格式     | JSON                              | 纯文本、CSV        |
| 记录方式 | logging 异步（asyncio.to_thread） | 同步写入、异步队列 |

**选择理由**：

- **JSON 格式**可以直接被日志收集系统（ELK、Datadog、Graylog）消费，无需额外解析
- **异步写入**不阻塞 LLM 调用的主流程 —— 日志延迟不应影响用户体验
- **单例模式**避免重复配置 logger

#### 输出示例

```json
{
  "timestamp": 1745923456.789,
  "model": "gpt-4o",
  "messages_count": 5,
  "total_tokens": 1234,
  "duration": 2.34,
  "success": true,
  "finish_reason": "stop"
}
```

---

### RateLimiter — 客户端限流

**文件**：`app/services/llm/rate_limiter.py`

#### 功能

使用双 Token Bucket 算法，同时限制：

- **RPM**（Requests Per Minute）—— 每分钟请求数
- **TPM**（Tokens Per Minute）—— 每分钟 Token 消耗量

#### Token Bucket 算法

```python
class TokenBucket:
    def __init__(self, capacity: float, refill_rate: float):
        self.capacity = capacity          # 桶容量（最大突发）
        self.refill_rate = refill_rate    # 每秒补充速率
        self._tokens = capacity           # 当前 Token 数

    async def acquire(self, tokens=1.0):
        # 等待直到 Token 足够
        if self._tokens < tokens:
            wait = (tokens - self._tokens) / self.refill_rate
            await asyncio.sleep(wait)
        self._tokens -= tokens
```

#### 为什么选择「Token Bucket」

| 算法                     | 特点                                                |
| ------------------------ | --------------------------------------------------- |
| **Token Bucket**（当前） | 允许突发 + 长期限流，桶容量积攒时能处理短时流量高峰 |
| 漏桶（Leaky Bucket）     | 请求以恒定速率流出，无法应对突发                    |
| 固定窗口计数器           | 窗口边界处可能出现双倍请求                          |
| 滑动窗口日志             | 精确但内存消耗大                                    |

**选择理由**：

- Token Bucket 在**允许突发**和**长期平滑**之间取得平衡 —— LLM 调用有时集中在一小段时间（Agent 并行工具调用），需要能处理突发
- 双桶设计（RPM + TPM）同时限制请求频率和 Token 消耗量
- 支持 `Retry-After` 响应头的处理：当 API 返回 429 + Retry-After 时，等待指定时间再重试

#### 其他可选的方法

1. **漏桶算法**：流量整形严格，适合对延迟敏感的场景，但不适合 Agent 的突发调用模式
2. **信号量 + 计数器**：通过 `asyncio.Semaphore` 限制并发数 + 计时器重置，实现简单但精度低
3. **滑动窗口**：精确记录过去 N 秒的请求数，内存消耗与窗口精度成正比

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

| 配置项                                 | 类型  | 默认值 | 说明                                                |
| -------------------------------------- | ----- | ------ | --------------------------------------------------- |
| `llm_max_retries`                      | int   | `2`    | 最大重试次数                                        |
| `llm_stream_max_retries`               | int   | `1`    | 流式整流重试次数（首 token 前中断才整流；`0`=禁用） |
| `llm_base_delay`                       | float | `1.0`  | 退避基数（秒）                                      |
| `llm_max_delay`                        | float | `30.0` | 退避上限（秒）                                      |
| `llm_use_jitter`                       | bool  | `True` | 是否启用随机抖动                                    |
| `llm_circuit_window_seconds`           | float | `10.0` | 滑动时间窗口长度（秒）                              |
| `llm_circuit_error_threshold`          | float | `0.5`  | 窗口内错误率熔断阈值（50%）                         |
| `llm_circuit_request_volume_threshold` | int   | `20`   | 窗口内最小请求量，不足不做错误率评估                |
| `llm_circuit_all_failed_min`           | int   | `3`    | 低流量纯失败保护：全部失败且达此样本量才熔断        |
| `llm_circuit_recovery_timeout`         | float | `30.0` | 熔断恢复超时（秒）                                  |
| `llm_circuit_half_open_max_requests`   | int   | `3`    | 半开状态最大探针数                                  |
| `llm_fallback_model_id`                | str   | `""`   | 降级备用模型                                        |
| `llm_proxy_url`                        | str   | `""`   | HTTP 代理                                           |
| `llm_main_rpm`                         | int   | `60`   | 主模型 RPM 限流                                     |
| `llm_reasoning_rpm`                    | int   | `30`   | 推理模型 RPM 限流                                   |
| `llm_fast_rpm`                         | int   | `100`  | 快速模型 RPM 限流                                   |

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
# 控制台友好格式
from app.services.llm import LLMLogger, LLMRequestRecord
record = LLMRequestRecord(...)
print(LLMLogger.format_for_console(record))
# 输出：[LLM] ✓ gpt-4o 1234 tokens 2.34s msgs=5
```

---

## 当前进度与遗留

> 本节记录 LLM 层自身的进度、遗留工作与下一步计划（项目整体进度见 [HANDOFF.md](../HANDOFF.md)）。

### 已实现

- 8 子模块全部落地：ClientManager / RetryHandler / StreamParser / StructuredOutput / LLMLogger / RateLimiter / CostTracker + `EmbeddingService`
- retry.py 工业级改造（2026-08-01）：滑动窗口熔断、错误分类白名单、半开探针失败一律回 OPEN、流式迭代保护（详见 [retry.md](retry.md)）
- **流式整流重试（2026-08-01）**：`async_generate()` 在**产出第一个 token 前**流中断时整流重试（重新 create + 重新迭代）；已产出 token 后中断不整流。详见下文「流式整流重试」小节

### 遗留未定事项

| 事项                                              | 当前状态           | 说明                                                                                                            |
| ------------------------------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------- |
| **RateLimiter 未集成**                            | 有代码无效果       | `rate_limiter.py` 已实现（双 Token Bucket，RPM+TPM），但 `LLMService.async_generate()` / `generate()` 均未调用  |
| **流式迭代是否自动重试**                          | ✅ 已决策（整流）  | 首 token 前中断整流重试（复用 `classify_error`，新增 `llm_stream_max_retries` 配置）；已产出 token 后中断不整流 |
| **`APIResponseValidationError` 是否容忍网关故障** | 保持 NON_RETRYABLE | 决策：当前直连服务商场景下重试无效，保持现状不修改                                                              |
| **`generate_structured` 重复实现**                | 两个入口           | `LLMService.generate_structured` 是简化版，`StructuredOutput.extract` 更完整（JSON mode 降级 + regex fallback） |

### 下一步计划

1. **集成 RateLimiter 到 LLMService**：`async_generate()` / `generate()` 在调用 ClientManager 前调用 `RateLimiter.acquire()`；settings 已有 `llm_main_rpm` / `llm_reasoning_rpm` / `llm_fast_rpm`
2. **决策遗留事项**：~~流式迭代是否自动重试~~（已决策，见下）——剩余 `APIResponseValidationError` 已决策保持；`generate_structured` 统一待评估
3. **统一结构化输出入口**：评估 `LLMService.generate_structured` 与 `StructuredOutput.extract` 的合并

---

## 流式整流重试

> 本节记录 `async_generate()` 流式迭代整流重试的前因后果（2026-08-01 实施）。

### 问题背景

`retry.execute()` 只保护 `client.chat.completions.create()`（创建响应对象），真正的 `async for chunk in response:` 迭代在重试范围外。流中途断掉（读超时 / 连接重置 / 解析失败）时，旧实现只捕获报错（记日志 + 错误事件），不重试。

### 工业调研结论（决策依据）

- **OpenAI Python SDK**：`max_retries` 只覆盖初始 HTTP 请求，不重试 mid-stream。官方维护者明确"用户已消费部分输出，mid-stream 重试语义不清晰"
- **LangChain `langchain-failover`**：只在主模型**产出第一个 token 前**死亡时 failover——"你永远不会得到重复的、半流输出"
- **awaken 运行时**：4 级恢复（ContinueText / SynthesizeToolUse / TruncateBeforeTool / WholeRestart），WholeRestart（整流重试）只在无文本、无完整工具调用时用

### 决策（用户确认）

1. **首 token 前中断 → 整流重试**（重新 create + 重新迭代）：用户没看到任何输出，整流不会产生重复内容
2. **已产出 token 后中断 → 不整流**：避免重复输出 / token 双倍计费 / tool_calls 残缺
3. **新增独立配置 `llm_stream_max_retries`**（默认 1），不复用 `llm_max_retries`——create 重试（HTTP 请求级）与整流重试（已开始流式后重启）属不同故障阶段，需独立调优；设为 `0` 即禁用整流，行为退化为旧版
4. **整流条件复用 `classify_error`**：只有 RETRYABLE / RATE_LIMITED 的迭代异常才整流；NON_RETRYABLE（4xx/校验错误/token 截断/未知）不整流——重试大概率无效，避免白打下游
5. **create 阶段异常绝不整流**：`retry.execute` 已决定重试/熔断/fallback；400 / `CircuitBreakerOpenError` / fallback 失败走既有错误路径
6. **cancel_event 置位永不整流**：迭代内取消检查 + 整流分支退避前后各一道守卫

### 实现要点

- `async_generate` 重构为显式 `for attempt in range(llm_stream_max_retries + 1)` 循环（async generator 无法用 `retry.execute` 直接包，因其 `call_fn` 返回 Awaitable 而非 AsyncGenerator）
- **`emitted_any` 标志**：`reasoning_token` / `message_token` / `tool_call_deltas` 任一非空即置位；`finish_reason` / `usage` 不算"首 token"（纯 usage/finish 死流仍可整流）
- **result 复用**：整流写同一 `result` 对象；`emitted_any=False` 保证 content/reasoning/tool_calls 未写，重试幂等安全。整流前清空死流的 `result.finish_reason` / `result.usage` 残留
- **退避**：复用 `llm_base_delay` / `llm_max_delay` / `llm_use_jitter`（`_stream_backoff` 辅助，公式与 create 阶段一致），不新增退避配置
- **日志**：每次尝试独立计时，失败走 `success=False` + error，成功清 `error=None`（整流成功 = 1 条失败 + 1 条成功日志）
- **fallback**：仍只绑定 create 阶段（`retry.execute` 内部），流式中断不触发额外 fallback

### 测试

`tests/unit/test_stream_rectify.py`（11 用例）：首 token 前中断整流成功 / 已产出后不整流 / 连续中断超上限 / cancel 不整流 / create 失败不整流 / 整流尝试中 create 失败 / tool_call 增量算已产出 / 仅 usage/finish 后中断整流 / 成功路径回归 / NON_RETRYABLE 迭代异常不整流 / 日志正确性。

### 遗留微调（可后续评估）

- **熔断观察盲区**：流式迭代失败不计入熔断窗口（现状）。若下游"create 正常但流频繁中途断开"，熔断器不感知。可后续把"放弃时的 RETRYABLE 迭代失败"喂给 `cb.record_failure()`

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
- 探针失败 → 熔断器继续保持开启，重置超时计时

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
