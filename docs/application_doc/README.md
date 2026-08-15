# 应用层与集成层说明文档

> **更新日期**：2026-08-15
> **文档定位**：应用层（`app/application/`）与集成层（`app/integration/`）的模块说明，覆盖模块概述、实现状态、各模块核心功能与使用示例。LLM 子包细节见 [LLM 层文档](../integration_doc/llm_doc/llm.md)。

---

## 📋 目录

- [模块概述](#模块概述)
- [模块实现状态总览](#模块实现状态总览)
- [核心调度链路](#核心调度链路)
- [SessionManager — 会话管理](#sessionmanager--会话管理)
- [ContextManager — 上下文管理](#contextmanager--上下文管理)
- [ToolService — 工具服务](#toolservice--工具服务)
- [TaskService — 任务调度](#taskservice--任务调度)
- [EmbeddingService — 文本向量化](#embeddingservice--文本向量化)
- [LLMService — LLM Facade](#llmservice--llm-facade)
- [MemoryService — 记忆服务（预留）](#memoryservice--记忆服务预留)
- [配置关联](#配置关联)
- [相关文档](#相关文档)

---

## 模块概述

### 核心功能

服务层是系统的**业务调度枢纽**，位于 API 层与核心层（Agent）之间，负责串起会话、上下文、工具、任务与模型通信：

- **会话管理**（`SessionManager`）：会话 CRUD + Redis 热缓存 + DB 持久化，支持分页 / 搜索 / 统计
- **上下文管理**（`ContextManager`）：从会话历史组装 messages，token 精确计数 + 超限截断
- **工具服务**（`ToolService`）：工具容器 / 执行 / 统计 / 钩子 / 内置工具装配（已合并原 ToolRegistry）
- **任务调度**（`TaskService`）：任务级并发信号量 + `run_agent()` 流式包装
- **文本向量化**（`EmbeddingService`）：单条 / 批量嵌入 + 内存缓存
- **LLM Facade**（`LLMService`）：流式 / 非流式生成、结构化输出、成本计算，组合 LLM 子包全部可靠性能力
- **记忆服务**（`MemoryService`）：预留空实现

### 模块结构

```
app/application/
├── session/session_manager.py  ← SessionManager 会话管理（Redis 热缓存 + DB 持久化）
├── context/context_manager.py  ← ContextManager 上下文组装 / token 截断
└── task/task_service.py        ← TaskService 任务级并发信号量

app/integration/
├── llm/                        ← LLMService + 子包（ClientManager / RetryHandler / StreamParser / StreamingRectifier / StructuredOutput / ReservationLimiter / CostTracker）
├── tools/                      ← ToolService 与 5 个内置工具
└── embedding_service.py        ← EmbeddingService 文本向量化

app/domain/memory/memory_service.py  ← MemoryService（❌ 预留，空文件）
```

### 设计原则

1. **单例装配**：`container` 持有全部服务实例，启动时经 `Container.initialize()` 统一初始化，关闭时统一清理；依赖注入 `get_tool_service` 等返回同一实例
2. **服务统一入口**：工具系统经 `ToolService` 对外（容器 + 执行 + 统计 + 装配合并一处），不再有独立 ToolRegistry
3. **Facade 模式**：`LLMService` 是 LLM 能力的唯一外部入口，`llm/` 子包内部组件不对外暴露
4. **降级容错**：单个基础设施（Redis / DB / 工具）初始化失败不影响整体启动，只记录警告并降级
5. **调度与执行解耦**：`TaskService` 决定「任务何时并发执行」，Agent 决定「单个任务如何执行」

### 依赖关系

```
API 层（chat / session 路由）
        │
        ▼
  ┌───────────────────────────────────────────┐
  │              服务层（本层）                │
  │                                           │
  │  SessionManager ◄──► ContextManager       │
  │       │                                   │
  │       ▼                                   │
  │  TaskService.run_agent()                  │
  │       │                                   │
  │       ▼                                   │
  │  ReActAgent（app/domain/）                │
  │   ├── LLMService ──► llm/ 子包 ──► OpenAI │
  │   └── ToolService ──► app/integration/tools/ ──► 执行 │
  │                                           │
  │  EmbeddingService ──► OpenAI /embeddings  │
  └───────────────────────────────────────────┘
        │
        ▼
  基础设施层（Redis / Database，由 container 直接管理）
```

服务层内部依赖关系：`ContextManager` 依赖 `SessionManager`；`LLMService` 依赖 `llm/` 子包；`TaskService` / `ToolService` / `EmbeddingService` 相对独立。

---

## 模块实现状态总览

| 文件 | 状态 | 核心类与方法 | 定位 |
| --- | --- | --- | --- |
| `session_manager.py` | ✅ 已实现（454 行） | `SessionManager`：`create_session` / `get_session` / `get_messages` / `add_message` / `delete_session` / `hard_delete_session` / `list_sessions` / `list_sessions_v2` / `_get_session_stats` | 会话生命周期 + Redis 热缓存 + DB 持久化 + 分页/搜索/统计 |
| `context_manager.py` | ✅ 已实现（135 行） | `ContextManager`：`build_messages` / `count_tokens` / `count_messages_tokens` / `_truncate_messages` | 组装 messages + token 计数 + 超限截断 |
| `tool_service.py` | ✅ 已实现（409 行） | `ToolService`：`register` / `unregister` / `get` / `execute` / `get_stats` / `get_all_stats_summary` / `init_default_tools`；`ToolStats` | 工具容器 / 执行 / 统计 / 钩子 / 装配（已合并原 ToolRegistry） |
| `task_service.py` | ✅ 已实现（68 行） | `TaskService`：`run_agent` / `max_concurrent` | 任务级并发信号量 + `run_agent()` 流式包装 |
| `embedding_service.py` | ✅ 已实现（141 行） | `EmbeddingService`：`embed` / `embed_batch` / `clear_cache` / `cache_size` | 文本向量化 + 批量 + 缓存 |
| `llm_service.py` | ✅ 已实现（608 行） | `LLMService`：`async_generate` / `generate` / `generate_structured` / `calculate_cost`；`StreamResult` | LLM 统一 Facade |
| `memory_service.py` | ❌ 预留（0 行） | — | 记忆服务（短期 + 向量库长期） |

---

## 核心调度链路

```
POST /api/chat/send
  → SessionManager（会话验证 + 存用户消息）
  → ContextManager.build_messages（构建 messages，token 计数/截断）
  → TaskService.run_agent()（任务级并发信号量）
      → ReActAgent._strategy_cycle()（ReAct 循环）
          → LLMService.async_generate()（流式 + 重试/熔断/限流/整流）
          → ToolService.execute()（工具级信号量 + 超时 + 重试）
  → SSE 事件流 → SessionManager.add_message（存 assistant 消息）
```

服务层在链路中承担三类职责：

- **入口编排**：`SessionManager` + `ContextManager` 负责「拿到会话 → 组装请求」
- **并发控制**：`TaskService`（任务维度）+ `ToolService`（工具维度）双信号量，保护 GPU / 服务器资源
- **能力调度**：`LLMService` 负责模型通信，`ToolService` 负责工具执行，`EmbeddingService` 负责向量化

---

## SessionManager — 会话管理

**文件**：`app/application/session/session_manager.py`

### 核心功能

1. **会话生命周期**：创建、查询、软删除 / 硬删除
2. **消息持久化**：存储 user / assistant 历史消息，支持分页读取
3. **Redis 热缓存**：`session:{id}` 缓存会话元数据（7 天 TTL），`user_sessions:{user_id}:page:{n}` 缓存会话列表第一页（30 秒 TTL），`session_stats:{id}` 缓存统计（60 秒 TTL），减少 DB 压力
4. **缓存穿透保护**：`get_session` 走「Redis → DB → 回写 Redis」的 cache-through 模式
5. **统计聚合**：消息数 / Token 总数 / 最后消息时间，优先从缓存取
6. **增强查询**：`list_sessions_v2` 支持关键词、日期范围、状态筛选、排序与总数统计

### 关键方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `create_session` | `(user_id, system_prompt=None, title=None) -> dict` | 生成 UUID，DB 持久化后预热 Redis 缓存 |
| `get_session` | `(session_id) -> dict \| None` | Redis → DB 缓存穿透保护，未命中回写 |
| `get_messages` | `(session_id, limit=50, offset=0) -> list[dict]` | 仅返回 user / assistant 角色，按时间升序，分页 |
| `add_message` | `(session_id, role, content, reasoning_content=None, token_count=0) -> int` | 写入消息记录，返回消息 ID |
| `delete_session` | `(session_id) -> None` | 软删除：删缓存 + DB 更新 `status="deleted"` |
| `hard_delete_session` | `(session_id) -> None` | 物理删除：先删消息（外键约束）再删会话，仅管理员/定时任务使用 |
| `list_sessions` | `(user_id, limit=20, offset=0, include_stats=True) -> list[dict]` | 活跃会话列表，第一页走 Redis 缓存 |
| `list_sessions_v2` | `(user_id, limit=20, offset=0, status="active", keyword=None, start_date=None, end_date=None, sort_by="updated_at", sort_order="desc", include_stats=True) -> tuple[list, int]` | 增强版：搜索 / 筛选 / 排序 / 总数统计 |
| `_get_session_stats` | `(session_id, db) -> dict` | 聚合查询消息数 / Token 数 / 最后消息时间 |

### 设计要点

- **降级策略**：Redis 不可用时打印 `[WARN]` 并缓存降级（直接查 DB）
- **参数防护**：`limit` 限制在 `[1, 100]`，`offset` 不小于 0
- **软删除默认**：`delete_session` 采用软删除（推荐），保留数据可回溯；物理删除仅限特殊场景
- **遗留注释**：文件内保留两份被注释掉的增强方案作为演进参考 —— 布隆过滤器 + 空值缓存（防穿透攻击）、游标分页（大规模数据，替换 OFFSET 分页）

### 使用示例

```python
# 创建会话
session = await container.session_manager.create_session(
    user_id="user-123",
    system_prompt="你是良率分析助手",
    title="RCA 分析",
)
session_id = session["id"]

# 查询（Redis → DB）
sess = await container.session_manager.get_session(session_id)

# 存取消息
await container.session_manager.add_message(session_id, "user", "分析这批不良率", token_count=42)
messages = await container.session_manager.get_messages(session_id, limit=20)

# 增强查询：按标题关键词搜索 + 总数分页
sessions, total = await container.session_manager.list_sessions_v2(
    user_id="user-123",
    keyword="RCA",
    sort_by="updated_at",
    sort_order="desc",
)
```

---

## ContextManager — 上下文管理

**文件**：`app/application/context/context_manager.py`

### 核心功能

1. **消息组装**：system prompt + 历史对话 + 当前用户输入，拼接为 LLM 可接受的 messages 格式
2. **Token 精确控制**：用 tiktoken 逐条计算消息与总上下文的 token 消耗，确保不超模型限制
3. **窗口管理**：超出 `max_context_tokens - max_output_tokens` 时，从最早的历史消息开始丢弃
4. **成本核算基础**：为每次请求提供 token 数据，供计费与监控

### 关键方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `count_tokens` | `(text: str) -> int` | 用 tiktoken 精确计算文本 token 数 |
| `count_messages_tokens` | `(messages: list[dict]) -> int` | 每条消息 +4 格式开销、`name` 额外 +1、末尾 +2 回复开销 |
| `build_messages` | `(session_id, user_message, max_rounds=20) -> tuple[list[dict], int]` | 组装完整 messages，返回 `(messages, total_tokens)` |
| `_truncate_messages` | `(messages, max_tokens) -> list[dict]` | 保留 system prompt 与最近对话，丢弃最早历史 |

### 组装策略

```
1. system prompt（session.system_prompt）
2. 历史消息（最近 max_rounds 轮，limit = max_rounds * 2）
3. 当前 user 消息
4. 计算 token；超过 available_tokens（max_context - max_output）→ 截断
```

- 编码器：按 `model_name` 解析 tiktoken encoder；未知模型回退 `cl100k_base`
- 截断只丢弃历史，始终保留 system prompt 和最新的 user 消息

### 使用示例

```python
messages, total_tokens = await container.context_manager.build_messages(
    session_id=session_id,
    user_message="继续分析不良数据",
    max_rounds=20,
)
# messages → [{"role": "system", ...}, {"role": "user", ...}, ...]
# total_tokens → 本次请求的预估 token 数
```

---

## ToolService — 工具服务

**文件**：`app/integration/tools/tool_service.py`

### 核心功能

1. **工具容器**：注册 / 注销 / 查询 / 列表（原 ToolRegistry 职责合并于此）
2. **工具执行**：`execute()` 带参数验证、自动重试（指数退避）、超时保护、执行统计、并发控制
3. **执行统计**：`ToolStats` 记录调用次数 / 成功率 / 平均耗时 / 最后调用时间
4. **钩子机制**：工具执行成功后运行扩展钩子（异步 / 同步皆可，钩子失败不影响工具执行）
5. **内置工具装配**：`init_default_tools()` 用 importlib 扫描 `app.tools.builtin` 包，幂等注册
6. **Schema 导出**：`get_openai_tools()` / `get_openai_responses()` 导出 OpenAI 格式工具列表

### 关键方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `register` | `(tool: BaseTool) -> None` | 注册工具，重名抛 `ValueError` |
| `unregister` | `(name: str) -> bool` | 注销工具及其统计 |
| `get` | `(name: str) -> BaseTool \| None` | 获取工具实例 |
| `list_tools` | `() -> list[str]` | 列出全部工具名 |
| `get_openai_tools` | `() -> list[dict]` | OpenAI Tool Schema 列表 |
| `get_openai_responses` | `() -> list[dict]` | OpenAI Response Schema 列表 |
| `execute` | `(name, parameters, timeout=None, max_retries=None, retry_delay=1.0) -> ToolResult` | 执行工具（信号量保护内） |
| `get_stats` | `(name=None) -> dict \| ToolStats \| None` | 单工具或全量统计 |
| `get_all_stats_summary` | `() -> dict` | 总调用 / 总成功率 / 各工具详情摘要 |
| `add_execution_hook` | `(hook: Callable) -> None` | 注册执行钩子 `async def hook(tool_name, parameters, result)` |
| `init_default_tools` | `() -> list[str]` | 注册全部内置工具，返回新增工具名列表 |

### 执行流程

```
execute(name, parameters)
  1. async with self._tool_semaphore（工具级并发信号量）
  2. 参数补默认（timeout=tool_timeout, max_retries=tool_max_retries）
  3. 查找工具（未注册 → 直接失败）
  4. 参数 JSON 字符串解析
  5. tool.validate_parameters() 前置验证
  6. 重试循环：asyncio.wait_for(tool.execute(**parameters), timeout)
     - 成功 → 记录统计 + 运行钩子 + 返回
     - 失败/超时 → 记录统计，指数退避 retry_delay × 2^attempt 后重试
```

**并发控制语义**：工具级信号量限制单任务内最大并发工具调用数（对应配置 `agent_max_concurrent_tools`），是 **Agent 维度（GPU / 服务器资源）**，而非 LLM API 维度（RPM / TPM 由 `reservation_limiter` 覆盖）。`async with` 天然保证异常 / 取消时释放信号量，不会挂死占坑。

### 使用示例

```python
# 执行内置工具（search / readFile / writeFile / code_exec / web_browse）
result = await container.tool_service.execute(
    name="search",
    parameters={"query": "良率 RCA 案例"},
    timeout=30,
    max_retries=3,
)

# 获取 OpenAI 格式工具列表（注入 LLM tools 参数）
tools = container.tool_service.get_openai_tools()

# 注册自定义钩子
async def log_hook(tool_name, parameters, result):
    print(f"[hook] {tool_name} -> {result.success}")

container.tool_service.add_execution_hook(log_hook)

# 统计摘要
summary = container.tool_service.get_all_stats_summary()
# → {"total_calls": ..., "overall_success_rate": ..., "tools": {...}}
```

---

## TaskService — 任务调度

**文件**：`app/application/task/task_service.py`

### 核心功能

1. **任务级并发信号量**：`asyncio.Semaphore` 限制同时运行的 Agent 任务数（对应配置 `agent_max_concurrent_tasks`）
2. **`run_agent()` 包装**：在信号量保护下运行 Agent，流式产出 SSE 事件

### 关键方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `run_agent` | `(user_input, messages, context, agent) -> AsyncGenerator[str]` | 信号量保护下运行 Agent，逐事件 yield |
| `max_concurrent` | `property -> int` | 当前最大并发任务数 |

### 设计要点

- **信号量位置**：在 `run_agent` 的 generator **外** acquire/release —— yield 会挂起 generator frame，若 acquire 放 generator 内，其他任务会在首个 yield 前交错进入，信号量失去约束
- **并发语义**：信号量是 **Agent 维度**（限制同时运行的 Agent 任务），而非 LLM API 维度（RPM / TPM 由 `reservation_limiter` 覆盖）
- `async with` 天然保证异常 / 取消时释放信号量

### 使用示例

```python
async def handle_chat(user_input, messages, context, agent):
    # 并发超限时在此等待
    async for event in container.task_service.run_agent(
        user_input=user_input,
        messages=messages,
        context=context,
        agent=agent,
    ):
        yield event  # SSE 事件字符串
```

---

## EmbeddingService — 文本向量化

**文件**：`app/integration/embedding_service.py`

### 核心功能

1. **单文本嵌入**：`embed(text)` 返回向量数组
2. **批量嵌入**：`embed_batch(texts)` 自动分批（每批 `max_batch_size` 条）
3. **内存缓存**：MD5 哈希键 + 模型名前缀去重，缓存仅在同一实例生命周期内有效
4. **缓存穿透优化**：只请求未命中的文本，结果保持输入顺序

### 关键方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `embed` | `(text, model=None) -> list[float]` | 单文本嵌入（内部调 `embed_batch`） |
| `embed_batch` | `(texts, model=None) -> list[list[float]]` | 批量嵌入，保持输入顺序 |
| `clear_cache` | `() -> None` | 清空缓存 |
| `cache_size` | `property -> int` | 缓存条目数 |
| `_make_cache_key` | `(text, model) -> str` | 缓存键：`"{model}:{md5(text)}"` |

### 批量流程

```
embed_batch(texts)
  1. 查缓存 → 命中项直接填结果，未命中项收集
  2. 未命中项按 max_batch_size 分批调 Embedding API
  3. 每批按输入顺序写回 results + 缓存
```

### 使用示例

```python
vector = await container.embedding_service.embed("这是什么产品")
# → [0.012, -0.034, ...] 共 1536 维

vectors = await container.embedding_service.embed_batch(
    ["文本一", "文本二", "文本三"],
)
```

---

## LLMService — LLM Facade

**文件**：`app/integration/llm/llm_service.py`

### 核心功能

1. **流式生成**（`async_generate`）：Agent 专用单轮流式生成，yield SSE 事件（reasoning / message / error）
2. **非流式生成**（`generate`）：适合简单任务的低延迟通道（默认 `fast` 模型）
3. **结构化输出**（`generate_structured`）：三级降级生成结构化 dict（JSON Schema → JSON Mode → 正则提取）
4. **成本计算**（`calculate_cost`）：按模型用量估算费用，代理 `CostTracker`
5. **可靠性集成**：组合 `RetryHandler`（重试 / 熔断 / fallback）、`ReservationLimiter`（客户端限流）、`StreamParser`（流式解析）、`log_event_async("llm_call")`（请求日志，全局框架）

### 关键数据结构

```python
class StreamResult:
    content: str                    # 完整回复文本
    reasoning_content: str          # 推理过程文本（如 DeepSeek-R1）
    finish_reason: str | None       # 停止原因（stop / length / tool_calls）
    tool_calls: list[dict]          # 完整工具调用列表
    usage: dict | None              # Token 用量
```

### 关键方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `async_generate` | `(messages, tools=None, temperature=0.2, max_tokens=4096, result=None, model_key="main", cancel_event=None) -> AsyncGenerator[str]` | 流式生成，yield SSE 事件 |
| `generate` | `(messages, tools=None, temperature=0, max_tokens=1024, response_format=None, model_key="fast") -> StreamResult \| None` | 非流式生成，失败返回 `None` |
| `generate_structured` | `(messages, schema, model_key="fast") -> dict \| None` | 结构化输出，三级降级（JSON Schema → JSON Mode → 正则） |
| `calculate_cost` | `(usage, model="") -> dict` | 静态方法，代理 `CostTracker.calculate` |

### 流式生成的可靠性编排

```
async_generate()
  1. 构建请求 kwargs + retry handler + fallback + log record
  2. 限流：_count_prompt_tokens 估算 token → ReservationLimiter.reserve()
  3. 整流重试循环（llm_stream_max_retries + 1 轮）：
     - 阶段 1：retry.execute(call_fn=_rate_limited_call, fallback_fn)
         · call_fn 每次真实请求前先 reserve，create 失败全额退（cancel）
         · retry 内部每次重试都重新 reserve
         · fallback 备用模型不参与 reserve（防主模型突发）
     - 阶段 2：逐 chunk 解析（StreamParser.parse_chunk）
         · reasoning → yield build_reasoning_event
         · message   → yield build_message_event
         · tool_call deltas → 累积 → merge_tool_calls
         · usage     → 写入 result.usage
         · cancel_event 置位 → settle 退差 + 优雅终止
  4. 整流判定（首 token 前中断 + 可恢复异常 + 未超上限 + 未取消）
     → 退避后重新 create + 迭代
  5. 结算：settle(actual) 退 TPM 差额；log_event_async("llm_call") 记录
```

**关键语义**：

- **限流闭环**：`reserve`（预扣）→ `settle(actual)`（退差）或 `cancel`（全额退），按「请求是否已发出」分界，避免 reservation 泄漏
- **整流重试**：已产出 token 后中断**不整流**（避免重复输出）；仅首 token 前中断且异常可恢复才整流
- **cancel_event**：硬取消（`CancelledError`）由 `finally` 兜底闭环，reservation 不泄漏

### 使用示例

```python
# 方式一：流式生成（SSE 事件）
sr = StreamResult()
async for event in llm.async_generate(
    messages=messages,
    tools=tools,
    model_key="main",
    result=sr,
):
    yield event  # 转发给前端

print(sr.content)      # 完整回复
print(sr.usage)        # Token 用量
print(sr.tool_calls)   # 工具调用（若 finish_reason == "tool_calls"）

# 方式二：非流式（简单任务，默认 fast 模型）
result = await llm.generate(
    messages=[{"role": "user", "content": "分类：今天天气很好"}],
    temperature=0,
)
print(result.content)

# 方式三：结构化输出
data = await llm.generate_structured(
    messages=[{"role": "user", "content": "张三，28岁"}],
    schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
# → {"name": "张三"}

# 成本计算
cost = LLMService.calculate_cost(
    usage={"prompt_tokens": 500, "completion_tokens": 200},
    model="gpt-4",
)
```

---

## MemoryService — 记忆服务（预留）

**文件**：`app/domain/memory/memory_service.py`（❌ 空文件，未实现）

### 定位

记忆服务规划为 Agent 的**长期 / 短期记忆层**，是架构文档中「核心层 Memory 预留」在服务层的对应入口。当前为占位文件，未提供任何实现。

### 规划方向

基于配置项（见「配置关联」中的记忆配置）规划两个方向：

1. **短期记忆**：会话内最近 N 条关键信息（`memory_max_short_term`，默认 10），随 ContextManager 组装 messages 时注入
2. **长期记忆**：向量化 + 向量库检索（`memory_vector_db` 可选 milvus / qdrant / pinecone，集合名 `memory_collection`），可复用 `EmbeddingService` 完成文本向量化，按语义检索历史经验后注入上下文

### 当前状态

- 服务层占位：`memory_service.py` 为 0 行空文件
- 文档占位：`docs/domain_doc/memory_doc/` 目录已建但无内容
- 开关：`memory_enabled` 默认 `False`

> 实现时需与 `ContextManager.build_messages` 集成（记忆注入点），并明确记忆写入 / 检索的触发时机（Agent 循环轮次、会话结束等）。

---

## 配置关联

服务层相关配置集中在 `app/config/settings.py`（详见 [config 文档](../config_doc/config.md)）：

| 配置项 | 默认值 | 关联模块 | 说明 |
| --- | --- | --- | --- |
| `max_context_tokens` | `128000` | ContextManager | 上下文 token 上限 |
| `max_output_tokens` | `4096` | ContextManager | 输出 token 预算 |
| `max_history_rounds` | `20` | ContextManager | 保留的最大历史轮数 |
| `agent_max_concurrent_tasks` | `10` | TaskService | 最大并发 Agent 任务数（1-100） |
| `agent_max_concurrent_tools` | `3` | ToolService | 单任务最大并发工具数 |
| `tool_timeout` | `30` | ToolService | 工具执行超时（秒） |
| `tool_max_retries` | `3` | ToolService | 工具执行最大重试次数 |
| `tool_max_output_length` | `100000` | ToolService | 工具输出最大字符数 |
| `tool_max_content_length` | `50000` | ToolService | 网页抓取最大字符数 |
| `redis_url` | `redis://localhost:6379/0` | SessionManager | Redis 连接 |
| `redis_session_ttl` | `604800` | SessionManager | 会话缓存 TTL（7 天） |
| `database_url` | `postgresql+asyncpg://...` | SessionManager | DB 连接 |
| `database_pool_size` / `database_max_overflow` | `20` / `10` | SessionManager | DB 连接池 |
| `llm_*` | 见 [LLM 文档](../integration_doc/llm_doc/llm.md) | LLMService / EmbeddingService | 模型 / 重试 / 熔断 / 限流等 |
| `llm_embedding_model_id` | `text-embedding-3-small` | EmbeddingService | 嵌入模型 |
| `llm_embedding_dimensions` | `1536` | EmbeddingService | 向量维度 |
| `memory_enabled` | `False` | MemoryService（预留） | 记忆总开关 |
| `memory_max_short_term` | `10` | MemoryService（预留） | 短期记忆条数 |
| `memory_vector_db` | `milvus` | MemoryService（预留） | 向量库类型（milvus/qdrant/pinecone） |
| `memory_collection` | `agent_memory` | MemoryService（预留） | 向量集合名 |

---

## 相关文档

- [架构设计](../architecture.md)（分层与核心链路）
- [LLM 层说明](../integration_doc/llm_doc/llm.md)（LLMService 底层 llm/ 子包详解）
- [Task 模块](task_doc/task.md)（任务调度顶层计划）
- [Session 模块](session_doc/session.md)（会话管理详解）
- [Context 模块](context_doc/context.md)（上下文管理详解）
- [ToolService 模块](../integration_doc/tool_service_doc/tool_service.md)（工具服务详解）
- [Embedding 模块](../integration_doc/embedding_doc/embedding.md)（向量化详解）
- [工具层说明](../integration_doc/tools_doc/tools.md)（BaseTool 与内置工具）
- [Agent 模块](../domain_doc/agent_doc/agent.md)（ReActAgent 循环，服务层的下游调用方）
- [API 模块](../api_doc/api.md)（服务层上游调用方）
- [配置说明](../config_doc/config.md)（全部配置项）
- [部署](../deployment.md)
