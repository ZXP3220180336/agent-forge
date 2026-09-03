# 配置管理模块 对外接口文档

> **对应代码**：`app/config/`（[settings.py](../../app/config/settings.py)）
> **更新日期**：2026-08-29
> **文档定位**：配置模块对外接口契约（`settings` 单例 + 聚合属性）与全项目配置项参考手册
> **实现状态**：✅ 已实现

---

## 📋 目录

- [配置管理模块 对外接口文档](#配置管理模块-对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [对外接口](#对外接口)
    - [聚合属性契约](#聚合属性契约)
    - [单例契约](#单例契约)
    - [最小调用示例](#最小调用示例)
  - [内部实现组织](#内部实现组织)
  - [字段验证规则](#字段验证规则)
  - [配置项详解](#配置项详解)
    - [1. 应用配置](#1-应用配置)
    - [2. API 配置](#2-api-配置)
    - [3. LLM 配置](#3-llm-配置)
    - [4. 上下文配置](#4-上下文配置)
    - [5. Agent 配置](#5-agent-配置)
    - [6. 记忆系统配置](#6-记忆系统配置)
    - [7. 数据库配置](#7-数据库配置)
    - [8. Redis 配置](#8-redis-配置)
    - [9. 工具配置](#9-工具配置)
    - [10. Tavily 配置](#10-tavily-配置)
    - [11. 日志配置](#11-日志配置)
    - [12. 监控配置](#12-监控配置)
    - [13. 安全配置](#13-安全配置)
  - [环境变量配置](#环境变量配置)
    - [加载机制](#加载机制)
    - [配置优先级](#配置优先级)
    - [.env 文件示例](#env-文件示例)
  - [配置消费导航](#配置消费导航)
  - [相关文档](#相关文档)

---

## 模块概述

配置管理模块统一管理全系统配置项：从环境变量（`.env` + 系统环境）加载，经 Pydantic 类型验证后以**单例**对外暴露，并提供按用途聚合的参数字典。

### 模块结构

```text
app/config/
├── __init__.py          # 导出 settings 单例（__all__ = ["settings"]）
└── settings.py          # Settings 类（配置字段 + 聚合属性 + 字段验证）+ get_settings 单例

.env                     # 环境变量配置文件（项目根，Pydantic Settings 自动加载）
```

### 设计原则

1. **单一配置源**：所有配置项集中在一个模块，从环境变量加载，不散落代码各处
2. **类型安全**：Pydantic 类型验证与自动转换，配置错误在启动时暴露
3. **默认值优先级**：`代码默认值 < .env 文件 < 系统环境变量`
4. **单例模式**：`get_settings()` 以 `@lru_cache` 缓存，全局仅一个实例，避免重复读取环境变量

### 依赖关系

- `pydantic-settings`（`BaseSettings`，Pydantic v2 独立包，需从 `pydantic_settings` 导入）
- 敏感信息（API Key / JWT 密钥）只经环境变量注入，禁止硬编码

---

## 对外接口

模块对外暴露的契约 = `settings` 单例上的**配置字段**（见 [配置项详解](#配置项详解)）与**聚合属性**。配置校验失败抛 `pydantic.ValidationError`（启动时暴露，调用方无需捕获）。

### 聚合属性契约

| 属性 | 返回 | 说明 |
| --- | --- | --- |
| `is_production` | bool | 生产环境判断（`not debug`） |
| `llm_config` | dict | 主模型参数字典（api_key / base_url / model / temperature / max_tokens / timeout=httpx.Timeout 分级实例） |
| `llm_client_timeout` | httpx.Timeout | 分级超时实例（connect/read/write/pool，装配根注入 `ClientManager.register_config(timeout=...)`；httpx `TimeoutTypes` 不接受 dict，故为实例） |
| `llm_reasoning_config` | dict | 推理模型参数字典（model 为空时回退主模型） |
| `llm_fast_config` | dict | 快速模型参数字典（model 为空时回退主模型） |
| `llm_embedding_config` | dict | 嵌入模型参数字典（api_key / base_url / model / dimensions） |
| `agent_config` | dict | Agent 运行参数字典（max_iterations / timeout / streaming / priority_levels / default_priority / high_priority_timeout / low_priority_timeout / priority_queue_size） |
| `concurrency_config` | dict | 并发控制参数字典（max_concurrent_tasks / max_concurrent_tools / task_queue_size / worker_pool_size） |
| `database_config` | dict | 数据库连接参数字典（url / pool_size / max_overflow / echo） |
| `redis_config` | dict | Redis 连接参数字典（url / session_ttl） |
| `memory_config` | dict | 记忆系统参数字典（enabled / max_short_term / vector_db / collection） |
| `tool_config` | dict | 工具执行参数字典（timeout / max_retries / max_output_length / max_content_length） |

### 单例契约

| 符号 | 类型 | 说明 |
| --- | --- | --- |
| `get_settings()` | `() -> Settings` | `@lru_cache` 单例工厂，首次调用创建并缓存 |
| `settings` | `Settings` | 全局配置实例（`app/config/__init__.py` 导出） |

### 最小调用示例

基本使用：

```python
from app.config import settings

# 访问单个配置项
api_key = settings.llm_api_key
model_id = settings.llm_model_id

# 判断环境
if settings.is_production:
    ...  # 生产环境逻辑
else:
    ...  # 开发环境逻辑
```

聚合配置注入：

```python
from app.config import settings

# 获取 LLM 配置字典，注入服务初始化
llm_config = settings.llm_config
agent_config = settings.agent_config

llm_service = LLMService(**settings.llm_config)
```

---

## 内部实现组织

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| 模块导出 | `__init__.py` | 导出 `settings` 单例（`__all__ = ["settings"]`） |
| `Settings` 类 | `settings.py` | 全部配置字段 + 聚合属性 + 字段验证器 |
| `get_settings()` | `settings.py` | `@lru_cache` 单例工厂 |
| `settings` 实例 | `settings.py` | 全局单例（`settings = get_settings()`） |

---

## 字段验证规则

| 验证器 | 字段 | 边界 |
| --- | --- | --- |
| `validate_temperature` | `llm_temperature` | 0 ≤ t ≤ 2 |
| `validate_max_iterations` | `agent_max_iterations` | 1 ≤ n ≤ 100 |
| `validate_max_cost` | `agent_max_cost` | ≥ 0（None=不启用） |
| `validate_max_empty_retries` | `agent_max_empty_retries` | ≥ 0 |
| `validate_max_llm_fail_retries` | `agent_max_llm_fail_retries` | ≥ 0 |
| `validate_max_same_action_turns` | `agent_max_same_action_turns` | ≥ 1 |
| `validate_max_refine_rounds` | `agent_max_refine_rounds` | 0 ≤ n ≤ 20 |
| `validate_concurrent_tasks` | `agent_max_concurrent_tasks` | 1 ≤ n ≤ 100 |
| `validate_queue_size` | `agent_priority_queue_size` | 1 ≤ n ≤ 10000 |
| `validate_embedding_dimensions` | `llm_embedding_dimensions` | > 0 |
| `validate_jwt_expire` | `jwt_expire_minutes` | 1 ≤ n ≤ 10080（1 分钟 ~ 7 天） |
| `validate_reserve_safety_margin` | `llm_reserve_safety_margin` | 1.0 ≤ v ≤ 4.0 |
| `validate_reserve_quantile` | `llm_reserve_quantile` / `llm_reserve_reasoning_quantile` | 0 < v < 1（开区间） |
| `validate_reserve_positive_int` | `llm_reserve_min_samples` / `llm_reserve_window` | ≥ 1 |

（13 组 `@field_validator`，覆盖 15 个字段域；越界抛 `ValueError` → Pydantic 汇总为 `ValidationError`）

---

## 配置项详解

### 1. 应用配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `APP_NAME` | str | "AI Agent System" | 应用名称，用于日志和监控 |
| `APP_VERSION` | str | "1.0.0" | 应用版本 |
| `DEBUG` | bool | false | 调试模式开关（`is_production = not debug`） |

### 2. API 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `API_PREFIX` | str | "/api" | API 路由前缀 |
| `CORS_ORIGINS` | list[str] | ["*"] | CORS 允许的源 |

### 3. LLM 配置

#### 3.1 主模型配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_KEY` | str | "" | LLM API 密钥（必填） |
| `LLM_BASE_URL` | str | `https://api.openai.com/v1` | API 端点 |
| `LLM_MODEL_ID` | str | "gpt-4" | 主模型标识符（用于主要对话） |
| `LLM_TEMPERATURE` | float | 0.2 | 生成温度（验证边界 0-2） |
| `LLM_MAX_TOKENS` | int | 4096 | 最大输出 Token 数 |
| `LLM_TIMEOUT_CONNECT` | float | 10 | 分级超时·连接（TCP+TLS 握手）上限（秒） |
| `LLM_TIMEOUT_READ` | float | 60 | 分级超时·读（相邻数据块空闲上限，含首字节等待——流空闲检测） |
| `LLM_TIMEOUT_WRITE` | float | 10 | 分级超时·写（请求体发送）上限 |
| `LLM_TIMEOUT_POOL` | float | 10 | 分级超时·连接池取连接等待上限 |
| `LLM_TIMEOUT_FIRST_TOKEN` | float | 60 | 首包阈值·首 chunk（首字节）等待上限（整流层看门狗，宽——覆盖模型思考） |
| `LLM_TIMEOUT_CHUNK_IDLE` | float | 15 | 空闲阈值·后续单 chunk 空闲上限（整流层看门狗，窄——判定断流） |
| `LLM_POOL_MAX_CONNECTIONS` | int | 100 | 连接池最大连接数（httpx.Limits） |
| `LLM_POOL_MAX_KEEPALIVE_CONNECTIONS` | int | 20 | 连接池最大保活连接数（httpx.Limits） |

> **连接期超时与池机制**（LLM-ADR-014）：
>
> - **分级超时**：connect/read/write/pool 四档组装为 `httpx.Timeout` 实例（`settings.llm_client_timeout`），经装配根注入 `ClientManager.register_config(timeout=...)` → openai `AsyncOpenAI`（httpx `TimeoutTypes` 不接受 dict，须实例）。read 档为传输层读兜底上限。
> - **连接池上限**：pool 字段触发 `http_client`（httpx.AsyncClient + `httpx.Limits`）注入——pool limits 只能经 http_client 传给 openai；代理路径同享。
> - **首包/空闲双阈值**：httpx read 档无法区分「首字节等待（思考慢）」与「chunk 空闲（断流）」，整流层逐 chunk `wait_for` 实现双看门狗（首包宽 `FIRST_TOKEN`、后续空闲窄 `CHUNK_IDLE`）；看门狗超时的空串 `TimeoutError` 回退类型名，保证 `result.error` 非空。
>
> 原单一 `LLM_TIMEOUT` 已拆分。

**温度建议**：

- `0.0-0.3`：确定性任务（代码生成、数据提取）
- `0.4-0.7`：平衡任务（对话、分析）
- `0.8-1.0`：创意任务（写作、头脑风暴）

#### 3.2 推理模型配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_REASONING_MODEL_ID` | str | "" | 推理模型标识符（空则使用主模型） |
| `LLM_REASONING_TEMPERATURE` | float | 0.7 | 推理温度 |
| `LLM_REASONING_MAX_TOKENS` | int | 8192 | 推理最大 Token 数 |

用于深度思考（如 DeepSeek-R1）。`llm_reasoning_config` 的 `model` 为空时回退主模型。

#### 3.3 快速模型配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_FAST_MODEL_ID` | str | "" | 快速模型标识符（空则使用主模型） |
| `LLM_FAST_TEMPERATURE` | float | 0.0 | 快速模型温度 |
| `LLM_FAST_MAX_TOKENS` | int | 2048 | 快速模型最大 Token 数 |

用于简单任务（文本分类、信息提取、简单问答），成本优先。

#### 3.4 嵌入模型配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_EMBEDDING_MODEL_ID` | str | "text-embedding-3-small" | 嵌入模型标识符 |
| `LLM_EMBEDDING_DIMENSIONS` | int | 1536 | 向量维度（验证边界 > 0） |

用于向量化存储、相似度搜索、记忆检索。

#### 3.5 结构化输出配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_STRUCTURED_MAX_TOKENS` | int | 2048 | 结构化输出三级降级（strict json_schema → json mode → 正则）的模型输出预算；首次/回喂/fallback 用此值，截断时扩 2 倍重试 1 次 |

调用方可通过 `generate_structured(max_tokens=...)` 按业务覆盖（None 则用此默认）。

#### 3.6 LLM 高级配置（重试 / 熔断 / 限流）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_MAX_RETRIES` | int | 2 | 单次请求最大重试次数（指数退避） |
| `LLM_STREAM_MAX_RETRIES` | int | 1 | 流式整流重试次数（首 token 前中断才整流；0=禁用） |
| `LLM_STREAM_MAX_CONTINUATIONS` | int | 1 | 半流续接轮次上限（已产出 content 中断带前缀续写，LLM-ADR-015；0=禁用） |
| `LLM_BASE_DELAY` | float | 1.0 | 重试退避基准延迟（秒） |
| `LLM_MAX_DELAY` | float | 30.0 | 重试退避最大延迟（秒） |
| `LLM_USE_JITTER` | bool | true | 退避是否加随机抖动 |
| `LLM_CIRCUIT_WINDOW_SECONDS` | float | 10.0 | 熔断滑动窗口长度（秒） |
| `LLM_CIRCUIT_ERROR_THRESHOLD` | float | 0.5 | 窗口内错误率熔断阈值（50%） |
| `LLM_CIRCUIT_REQUEST_VOLUME_THRESHOLD` | int | 20 | 窗口内最小请求量，不足不做错误率评估 |
| `LLM_CIRCUIT_ALL_FAILED_MIN` | int | 3 | 低流量纯失败保护：全部失败且达此样本量才熔断 |
| `LLM_CIRCUIT_RECOVERY_TIMEOUT` | float | 30.0 | 熔断恢复超时（秒） |
| `LLM_CIRCUIT_HALF_OPEN_MAX_REQUESTS` | int | 3 | 半开探针最大放行请求数 |
| `LLM_FALLBACK_MODEL_ID` | str | "" | 主模型降级备用模型（空则无 fallback） |
| `LLM_PROXY_URL` | str | "" | HTTP 代理地址 |
| `LLM_MAIN_RPM` / `LLM_REASONING_RPM` / `LLM_FAST_RPM` | int | 60 / 30 / 100 | 三档模型客户端限流 RPM 配额 |
| `LLM_MAIN_TPM` / `LLM_REASONING_TPM` / `LLM_FAST_TPM` | int | 2000000 | 三档模型 TPM 配额（双 Token Bucket） |

熔断基于滑动时间窗口 + 错误率判定（Hystrix 模型），TPM 与 RPM 组成双桶限流，默认参考 DeepSeek 官方限额。具体机制见 [retry.md](../integration_doc/llm_doc/retry.md) 与 [limiter.md](../integration_doc/llm_doc/limiter.md)。

#### 3.7 LLM 自适应预留配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_ADAPTIVE_RESERVE` | bool | false | 自适应预留开关（Fenic 式 OutputTokenEstimator，默认关闭） |
| `LLM_RESERVE_QUANTILE` | float | 0.95 | 普通模型输出分位数（p95） |
| `LLM_RESERVE_REASONING_QUANTILE` | float | 0.99 | 推理模型输出分位数（p99，推理输出有相关性突发尖峰） |
| `LLM_RESERVE_SAFETY_MARGIN` | float | 1.15 | 预留安全系数（验证边界 1.0-4.0） |
| `LLM_RESERVE_MIN_SAMPLES` | int | 30 | 冷启动阈值：样本不足用静态上限 |
| `LLM_RESERVE_WINDOW` | int | 256 | 滚动样本窗口（deque 上限） |

开启后用「历史实际输出的高分位 × 安全系数」替代固定 `max_tokens` 预留，减少预留期间占桶（并发空耗）。详见 [limiter.md](../integration_doc/llm_doc/limiter.md)「对比 3.2」。

### 4. 上下文配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `MAX_CONTEXT_TOKENS` | int | 128000 | 最大上下文 Token 数 |
| `MAX_OUTPUT_TOKENS` | int | 4096 | 最大输出 Token 数 |
| `MAX_HISTORY_ROUNDS` | int | 20 | 保留历史对话轮数 |

### 5. Agent 配置

#### 5.1 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `AGENT_MAX_ITERATIONS` | int | 10 | 最大迭代次数（验证边界 1-100，防止死循环） |
| `AGENT_TIMEOUT` | int | 300 | ReAct 循环总时间上限（秒，5 分钟） |
| `AGENT_STREAMING` | bool | true | 是否启用流式输出 |
| `AGENT_MAX_CONTEXT_ROUNDS` | int | 8 | Agent 循环保留最近轮数（上下文预算，0/None 走 AgentContext 默认） |
| `AGENT_MAX_COST` | float \| None | 空 | 成本上限（美元 USD）；空=不启用，0 则任何正成本即停（ReAct 累计成本超限自动停机） |
| `AGENT_MAX_EMPTY_RETRIES` | int | 2 | 连续空输出重试上限：空输出最多重试 N 次，第 N+1 次仍空输出则终止；0=首次空输出即终止（防模型空转烧钱） |
| `AGENT_MAX_LLM_FAIL_RETRIES` | int | 2 | LLM 失败重试上限：LLM 调用失败最多重试 N 次，第 N+1 次仍失败则终止；0=首次失败即终止（对齐空输出护栏，防 handler CONTINUE 无限重试烧钱） |
| `AGENT_MAX_SAME_ACTION_TURNS` | int | 3 | 循环停滞检测：连续相同工具调用（工具+参数）超过 N 轮，下一轮仍相同则终止（防死循环） |

#### 5.2 任务优先级配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `AGENT_PRIORITY_LEVELS` | list[Literal["low", "normal", "high", "urgent"]] | ["low", "normal", "high", "urgent"] | 优先级等级 |
| `AGENT_DEFAULT_PRIORITY` | Literal["low", "normal", "high", "urgent"] | "normal" | 默认优先级 |
| `AGENT_HIGH_PRIORITY_TIMEOUT` | int | 600 | 高优先级任务超时时间（秒，10 分钟） |
| `AGENT_LOW_PRIORITY_TIMEOUT` | int | 180 | 低优先级任务超时时间（秒，3 分钟） |
| `AGENT_PRIORITY_QUEUE_SIZE` | int | 100 | 优先级队列大小（验证边界 1-10000） |

**优先级使用场景**：

- **urgent**：实时对话、紧急任务
- **high**：重要但非紧急的分析任务
- **normal**：常规任务（默认）
- **low**：后台批处理任务

#### 5.3 并发控制配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `AGENT_MAX_CONCURRENT_TASKS` | int | 10 | 最大并发任务数（验证边界 1-100） |
| `AGENT_MAX_CONCURRENT_TOOLS` | int | 3 | 单个任务最大并发工具数 |
| `AGENT_TASK_QUEUE_SIZE` | int | 50 | 任务队列大小 |
| `AGENT_WORKER_POOL_SIZE` | int | 5 | 工作线程池大小 |

落地：`AGENT_MAX_CONCURRENT_TASKS` → `TaskService` 任务级信号量；`AGENT_MAX_CONCURRENT_TOOLS` → `ToolService` 工具级信号量。信号量用 `async with` 管理，异常/取消时自动释放。详见 [task.md](../application_doc/task_doc/task.md) 与 [tool_service.md](../integration_doc/tools_doc/tool_service.md)。

### 6. 记忆系统配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `MEMORY_ENABLED` | bool | false | 是否启用长期记忆 |
| `MEMORY_MAX_SHORT_TERM` | int | 10 | 短期记忆保留条数 |
| `MEMORY_VECTOR_DB` | Literal["milvus", "qdrant", "pinecone"] | "milvus" | 向量数据库类型 |
| `MEMORY_COLLECTION` | str | "agent_memory" | 集合名称 |

### 7. 数据库配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DATABASE_URL` | str | `postgresql+asyncpg://user:pass@localhost/db` | 数据库连接字符串 |
| `DATABASE_POOL_SIZE` | int | 20 | 连接池大小 |
| `DATABASE_MAX_OVERFLOW` | int | 10 | 最大溢出连接数 |
| `DATABASE_ECHO` | bool | false | 是否打印 SQL |

### 8. Redis 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `REDIS_URL` | str | `redis://localhost:6379/0` | Redis 连接字符串 |
| `REDIS_SESSION_TTL` | int | 604800 | 会话过期时间（秒，默认 7 天） |

### 9. 工具配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TOOL_TIMEOUT` | int | 30 | 工具执行超时时间（秒） |
| `TOOL_MAX_RETRIES` | int | 3 | 工具执行最大重试次数 |
| `TOOL_MAX_OUTPUT_LENGTH` | int | 100000 | 工具输出截断长度（字符数，code_exec / readFile） |
| `TOOL_MAX_CONTENT_LENGTH` | int | 50000 | 网页抓取最大内容长度（字符数，web_browse） |
| `TOOL_ALLOWED_DIRS` | tuple[str, ...] | 项目根 | 文件工具允许目录白名单（默认项目根） |
| `TOOL_HTTP_TIMEOUT` | float | 15.0 | external http_api 示例工具请求超时（秒） |

**使用场景**：

- `TOOL_TIMEOUT`：单个工具执行不能超过此时间，通过 `asyncio.wait_for` 实现
- `TOOL_MAX_RETRIES`：失败后自动重试，配合渐进式退避策略（1s, 2s, 4s...）
- `TOOL_MAX_OUTPUT_LENGTH`：影响 `readFile` 和 `code_exec` 的输出截断
- `TOOL_MAX_CONTENT_LENGTH`：影响 `web_browse` 的网页内容截断
- `TOOL_ALLOWED_DIRS`：`readFile` / `writeFile` 允许目录白名单，白名单外路径拒绝访问（`..` 穿越与大小写经规范化处理），示例：`TOOL_ALLOWED_DIRS=["/data/yield"]`
- `TOOL_HTTP_TIMEOUT`：external `http_api` 示例工具请求超时（经 loader 配置注入 `CONFIG_KEYS`，见 [external.md](../integration_doc/tools_doc/external.md)）

### 10. Tavily 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TAVILY_API_KEY` | str | "" | Tavily API 密钥 |
| `TAVILY_SEARCH_DEPTH` | Literal["basic", "advanced"] | "basic" | 搜索深度 |

### 11. 日志配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LOG_LEVEL` | Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | "INFO" | 日志级别 |
| `LOG_FORMAT` | Literal["json", "text"] | "json" | 日志格式 |
| `LOG_FILE` | str | "logs/app.log" | 日志文件路径 |

> `LOG_*` 由全局日志框架（`app/platform/observability/logger.py` 的 `setup_logging()`）读取生效，见 [logging.md](../platform_doc/observability/logging.md)。

### 12. 监控配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `METRICS_ENABLED` | bool | false | 是否启用监控 |
| `METRICS_PORT` | int | 9090 | Prometheus 指标端口 |

### 13. 安全配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `JWT_SECRET_KEY` | str | "your-secret-key-change-in-production" | JWT 密钥 |
| `JWT_ALGORITHM` | Literal["HS256", "HS384", "HS512", "RS256"] | "HS256" | JWT 算法 |
| `JWT_EXPIRE_MINUTES` | int | 1440 | JWT 过期时间（分钟，验证边界 1-10080） |

> 生产环境必须修改 `JWT_SECRET_KEY`，使用强密钥（至少 32 字符随机字符串）。

---

## 环境变量配置

### 加载机制

- `Settings` 的 `model_config`：`env_file=".env"`、`env_file_encoding="utf-8"`、`case_sensitive=False`（环境变量名大小写不敏感）、`extra="allow"`（允许未声明字段）
- 全部配置项用 `settings` 单例读取；`.env` 由 Pydantic Settings 加载，**不写入** `os.environ`，不要用 `os.getenv()` 读 `.env` 中的配置

### 配置优先级

```text
系统环境变量 > .env 文件 > 代码默认值
```

### .env 文件示例

```bash
# ===== LLM 主模型配置 =====
LLM_API_KEY="sk-xxx"
LLM_BASE_URL="https://api.deepseek.com"
LLM_MODEL_ID="deepseek-v4-pro"
LLM_TEMPERATURE=0.2

# ===== LLM 推理模型配置 =====
LLM_REASONING_MODEL_ID="deepseek-reasoner"
LLM_REASONING_TEMPERATURE=0.7

# ===== Agent 并发配置 =====
AGENT_MAX_CONCURRENT_TASKS=20
AGENT_MAX_CONCURRENT_TOOLS=5

# ===== 数据库配置 =====
DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db"
REDIS_URL="redis://localhost:6379/0"
```

> 环境变量命名：全大写 + 下划线分隔（如 `LLM_API_KEY`）；`BaseSettings` 从 `pydantic_settings` 导入（Pydantic v2 已独立成包）。

---

## 配置消费导航

| 配置组 | 消费模块 | 文档 |
| --- | --- | --- |
| LLM 配置（`llm_*`） | `app/integration/llm/` | [llm.md](../integration_doc/llm_doc/llm.md) |
| 上下文配置（`max_*`） | `app/application/context/` | [context.md](../application_doc/context_doc/context.md) |
| Agent 配置（`agent_*`） | `app/domain/agent/` + `app/application/task/` | [agent.md](../domain_doc/agent_doc/agent.md) · [task.md](../application_doc/task_doc/task.md) |
| 记忆配置（`memory_*`） | `app/domain/memory/` | [memory.md](../domain_doc/memory_doc/memory.md) |
| 数据库 / Redis 配置 | `app/container.py` 直管 + `app/infrastructure/` | [infrastructure.md](../infrastructure_doc/infrastructure.md) |
| 工具配置（`tool_*`） | `app/integration/tools/` | [tools.md](../integration_doc/tools_doc/tools.md) |
| Tavily 配置 | `app/integration/tools/builtin/search.py` | [builtin.md](../integration_doc/tools_doc/builtin_doc/builtin.md) |
| 日志配置（`log_*`） | `app/platform/observability/logger.py` | [logging.md](../platform_doc/observability/logging.md) |
| 监控配置（`metrics_*`） | `app/platform/observability/metrics.py` | [observability.md](../platform_doc/observability.md) |
| 安全配置（`jwt_*`） | `app/api/middleware/` | [middleware.md](../api_doc/middleware_doc/middleware.md) |

---

## 相关文档

- [architecture.md](../architecture.md)（分层与演进）
- [deployment.md](../deployment.md)（部署与启动）
- 各层 README：`domain` / `application` / `integration` / `api`
- 各消费模块文档（见 [配置消费导航](#配置消费导航)）
