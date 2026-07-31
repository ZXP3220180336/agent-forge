# 🚩 项目交接文档

> **项目名称**：AI Agent 系统（AsyncioDemo）
> **交接时间**：2026-07-31
> **项目路径**：`e:\MyWorkSpace\Agent\VSCodeDemo\PersonalProject\AsyncioDemo`
> **运行方式**：`uv run python -m app.main`（FastAPI 服务）
> **Python**：3.14 | **包管理**：uv | **平台**：Windows 11
> **代码规模**：约 5040 行 Python

---

## 1. 我们在做什么

构建一个**工业级 AI Agent 系统**，基于 FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整的 ReAct 循环 Agent。

### 架构分层

```
API 层（FastAPI 路由）
    ↓
服务层（LLMService / SessionManager / ContextManager / MemoryService / TaskService / ToolService）
    ├── LLM 子包（ClientManager / RetryHandler / StreamParser / RateLimiter / CostTracker / LLMLogger / StructuredOutput）
    └── EmbeddingService
    ↓
核心层（Agent / Reasoning / Memory / Prompts）
    ├── BaseAgent.run() → _strategy_cycle()
    ├── ReActAgent（推理 ↔ 工具 ↔ 推理循环）
    ├── 预留策略接口（Plan-then-Execute / Reflection）
    └── 提示词模板（系统 / 工具 / 规划）
    ↓
事件层（app/core/events.py）← SSE 事件定义
    ↓
基础设施层（Database / Redis / VectorStore / MessageQueue）
    ↓
工具层（搜索 / 文件 / 代码执行 / 网页抓取）
    ↓
配置层（Pydantic Settings）← 全部通过 settings 单例访问
```

### 任务计划

| Phase | 模块 | 状态 |
|-------|------|------|
| 1 | 配置模块 + 工具模块 | ✅ 已完成 |
| 2 | LLM 服务层重构（8 子模块） | ✅ 已完成（本轮修复瑕疵） |
| 3 | 核心 Agent 层（BaseAgent + ReActAgent + Prompts + Events） | ✅ 已完成（本轮修复 tool_calls 配对，接入 chat_router，闭环打通） |
| 4 | 基础设施层（Database/Redis/VectorStore/MessageQueue） | 🔶 有文件但未验证（asyncpg 驱动未安装，DB 恒降级） |
| 5 | 服务层补全（ToolService / MemoryService / TaskService） | 🔶 ToolService 已实现；Memory/Task 仍为空文件 |
| 6 | API 路由完善（Admin / Agent / Tool 路由） | 🔶 chat 已接入 ReActAgent；Admin/Agent/Tool 路由仍为空文件 |
| 7 | 测试 + 文档收尾 | 🔶 部分完成（新增 chat 桥接集成测试，pytest 已接入） |

---

## 2. 已完成的工作

### 2.1 配置模块

**路径**：`app/config/settings.py` → 导出 `settings` 单例

**核心能力**：
- 13 组配置项，Pydantic 类型验证
- 多模型切换（主 / 推理 / 快速 / 嵌入）
- 任务优先级 + 并发控制配置
- 6 个字段验证器，11 个聚合属性
- LLM 高级配置：重试、熔断、限流、Fallback、抖动开关
- **+ 新增** `llm_circuit_half_open_max_requests`（半开探针数，默认 3）

**文档**：`docs/config.md`

### 2.2 工具模块

**路径**：`app/tools/`

| 组件 | 文件 | 说明 |
|------|------|------|
| `BaseTool` | `base.py` | 抽象基类，4 个抽象方法 |
| `ToolResult` | `base.py` | 执行结果（含 execution_time / retry_count） |
| `ToolRegistry` | `registry.py` | 注册中心 + ToolStats 统计 |
| `SearchTool` | `builtin/search.py` | 网络搜索（Tavily，`asyncio.to_thread` 异步化） |
| `ReadFileTool` | `builtin/file_ops.py` | 读文件（aiofiles，带截断） |
| `WriteFileTool` | `builtin/file_ops.py` | 写文件（自动创建父目录） |
| `CodeExecTool` | `builtin/code_exec.py` | 终端命令（危险命令黑名单，异步子进程） |
| `WebBrowseTool` | `builtin/web_browse.py` | 网页抓取（httpx 连接池复用，自实现 HTML Parser） |

**文档**：`docs/tools.md`

### 2.3 LLM 层 ⭐

**路径**：`app/services/llm_service.py`（Facade）+ `app/services/llm/`（8 个子模块）

```
LLMService（统一 Facade）
    ├── async_generate()    ← 流式单轮生成（供 Agent 层）
    ├── generate()          ← 非流式单轮生成（简单任务）
    └── generate_structured() ← 结构化输出（JSON Schema）
    │
    ├── ClientManager       ← 全局连接池（main / reasoning / fast）
    ├── RetryHandler        ← 重试 + 熔断 + Fallback
    ├── StreamParser        ← 流式/非流式响应解析
    ├── StructuredOutput    ← 三级降级结构化输出
    ├── LLMLogger           ← JSON 结构化日志
    ├── RateLimiter         ← 双 Token Bucket 限流（⚠️ 未被集成）
    └── CostTracker         ← 按模型定价计算成本
```

#### 本轮修复（2026-07-30 ~ 07-31）

| 文件 | 问题 | 修复 |
|------|------|------|
| `llm/structured.py` | `_try_extract()` 和 `_fallback_extract()` 传参 `model=` 但 `generate()` 形参是 `model_key=` | 统一改为 `model_key`，删除无用变量 |
| `llm/streaming.py` | `ToolCallDelta.index` 缺类型注解 | 加 `: int` |
| `llm/client.py` | `**extra` 被静默吞没、`register_config` 不关闭旧 client、`close_all` 不关连接池 | 见 `docs/llm_client.md` |
| `llm/retry.py` | 死代码枚举、熔断不计 RETRYABLE、半开探针 off-by-one、探针 1 次成功即关闭 | 见 `docs/llm/retry.md` |
| `llm_service.py` | 死代码 `type("response_format", (), {})()` | 删除 |
| `core/agent/base.py` | `__init__` 参数缺类型注解 | 补上 |
| `core/agent/executor.py` | `__init__` 参数缺注解 + 缺 `LLMService`/`ToolRegistry` 导入 | 补上 |

**文档**：
- `docs/llm.md` — LLM 层总览
- `docs/llm_client.md` — ClientManager 设计（连接池、懒加载、优雅关闭）
- `docs/llm/retry.md` — RetryHandler 设计（流程图、计数推演、配置组合策略）

### 2.4 核心 Agent 层

**路径**：`app/core/agent/`

| 文件 | 组件 | 说明 |
|------|------|------|
| `base.py` | `BaseAgent` | 抽象基类，`run()` 统一入口 + `_strategy_cycle()` 策略接口 |
| `base.py` | `AgentContext` | 上下文（session_id / user_id / max_iterations / temperature） |
| `base.py` | `AgentResult` | 执行结果（content / reasoning / tool_calls / iterations / total_tokens / usage） |
| `base.py` | `AgentState` | 状态枚举（IDLE / THINKING / WAITING / COMPLETED / FAILED / CANCELLED） |
| `executor.py` | `ReActAgent` | ReAct 实现：LLM 推理 → finish_reason 判断 → tool_calls 执行 → 循环 |
| `planner.py` | (预留) | Plan-then-Execute |
| `reasoning.py` | (预留) | Reflection |

**文档**：`docs/agent.md`

### 2.5 其他模块

| 模块 | 状态 |
|------|------|
| 事件层 `app/core/events.py` | ✅ 7 种事件类型 + 统一构建函数 |
| 提示词 `app/core/prompts/` | ✅ 系统/工具/规划模板 |
| EmbeddingService | ✅ 向量化服务 |
| 记忆系统 `app/core/memory/` | ✅ 有文件（short/long/working）|
| 推理模块 `app/core/reasoning/` | ✅ 有文件（CoT/ReAct/Reflection）|
| 数据模型 `models/database/` | ✅ 有文件（base/messages/session/task/tool_log）|
| 基础设施 `infrastructure/` | ✅ 有文件（database/redis/vector_store/message_queue）|
| 服务层 `services/` | ✅ 有文件（tool/memory/task/context/session）|
| API 路由 `api/routes/` | ✅ 有文件（chat/session/admin/agent/tool）|
| 中间件 `api/middleware/` | ✅ 有文件（auth/rate_limit/error_handler）|
| 工具 utils/ | ✅ 有文件（logger/metrics/exceptions/helpers）|
| 应用状态 `app_state.py` | ✅ 容错初始化 |
| 入口 `main.py` | ✅ 路径修复 |

---

## 3. 当前卡在哪

### ✅ 问题 1（已解决）：chat_router → ReActAgent 闭环打通

`app/api/routes/chat.py` 的 `send_message` 已改为经 `ReActAgent.run()` 驱动：
`ContextManager.build_messages()` → `ReActAgent(llm, tools).run()` → SSE 事件流 → `agent.result` 保存回复。
配套改动：
- `app/services/tool_service.py`（空文件 → 实现 ToolService）
- `app/app_state.py` 启动时经 `ToolService.init_default_tools()` 注册 5 个内置工具（幂等）
- `app/dependencies.py` 新增 `get_tool_registry` 依赖注入

已用真实 API（DeepSeek + Tavily）验证完整闭环：工具调用 → 工具结果回传 → LLM 总结答复。

### 🟡 问题 2：端到端验证（部分缓解）

- `tests/integration/test_chat_flow.py` 已用 Fake LLM 覆盖桥接逻辑（不依赖外部 API / DB），pytest 已接入（pyproject 增加 pytest / pytest-asyncio）
- 真实 API 冒烟可跑：`uv run python -m scripts.test_agent`（需 LLM_API_KEY / TAVILY_API_KEY）
- 本地缺 Redis 和 PostgreSQL：`app_state.initialize()` 走降级路径。**注意**：`asyncpg` 驱动未安装（`No module named 'asyncpg'`），即使有 PostgreSQL 也无法连接 → 需在 pyproject 依赖中加 `asyncpg`

### 🟡 问题 3：RateLimiter 独立存在但未被集成

`app/services/llm/rate_limiter.py` 已实现（双 Token Bucket，RPM + TPM），但 `LLMService.async_generate()` 和 `generate()` 中都没有调用它。限流功能处于"有代码无效果"状态。

### 🟡 问题 4：部分模块有文件但未验证

许多模块仍然存在但未验证：
- `services/memory_service.py` / `task_service.py` → **空文件**
- `api/routes/admin.py` / `agent.py` / `tool.py` → **空文件**
- `infrastructure/` 下 database / redis / vector_store 未验证
- `core/reasoning/`、`core/memory/` 未验证

---

## 4. 下一步计划

### ✅ 优先 1（已完成）：打通 chat_router → ReActAgent 桥接

系统已形成"用户输入 → LLM 思考 → 工具调用 → LLM 总结 → 回复用户"的完整闭环，真实 API 验证通过。新增 `tests/integration/test_chat_flow.py` 覆盖（Fake LLM，无外部依赖）。

### 优先 2：集成 RateLimiter 到 LLMService

`async_generate()` 和 `generate()` 在调用 ClientManager 前应调用 `RateLimiter.acquire()`，防止触发 API 限流。settings 已有 `llm_main_rpm` / `llm_reasoning_rpm` / `llm_fast_rpm`。

### 优先 3：验证基础设施+服务层模块

- **先补依赖**：`pyproject.toml` 加 `asyncpg`（当前 `No module named 'asyncpg'`，DB 恒降级）
- 逐个验证 `infrastructure/`、`services/` 下现有文件：`memory_service.py` / `task_service.py` 为空文件
- `core/reasoning/`、`core/memory/` 未验证

### 优先 4：补全缺失模块

| 模块 | 优先级 | 说明 |
|------|--------|------|
| `api/routes/admin.py` / `agent.py` / `tool.py` | 🟡 | 空文件（tool 路由可基于 ToolService 的 stats 实现） |
| `docs/architecture.md` | 🟢 | 架构文档（空） |
| `docs/api.md` | 🟢 | API 文档（空） |
| `docs/deployment.md` | 🟢 | 部署文档（空） |
| 单元测试 | 🟢 | 已有集成测试，单元覆盖仍少 |

---

## 5. ⚠️ 踩过的坑（绝对不要再踩）

### 坑 1：文件名错误

**`app/models/__init__.py` 写成了 `__ini__.py`（少了个 t），导入时报 `ImportError: cannot import name 'XX' from 'app.models' (unknown location)`。**

排查了 5 轮才找到。出现 `(unknown location)` 的导入报错，**先检查 `__init__.py` 文件名是否正确**。

### 坑 2：SQLAlchemy 保留属性 `metadata`

**ORM 模型中 `metadata = Column(JSON)` 导致 `'metadata' is reserved when using the Declarative API` 运行时错误。** 改名为 `meta`。

### 坑 3：两个 `declarative_base()` 实例

**`messages.py` 和 `session.py` 各自 `Base = declarative_base()`，导致 FK 引用时 mapper 冲突。** 统一到 `models/database/base.py` 共享一个 Base。

### 坑 4：路由导入路径与文件结构不一致

**`api/routes/` 的文件名是 `chat.py`，但 `__init__.py` 写的是 `from .chat_router import ...`。路由内用 `from ..dependencies import ...`，但 `dependencies.py` 在 `app/` 下。** 解决：`__init__.py` 的导入名匹配实际文件名；路由内用绝对导入 `from app.dependencies import ...`。

### 坑 5：测试脚本运行时路径问题

**`uv run .\scripts\test_xxx.py` 会将 `scripts/` 加入 sys.path，找不到 `app` 模块。** 一律用 `uv run python -m scripts.test_xxx` 从项目根目录运行。

### 坑 6：Python 2 `except` 语法

**`except json.JSONDecodeError, IndexError:` 在 Python 3 中崩溃。** 必须用 `except (JSONDecodeError, IndexError):`。

### 坑 7：Pydantic v2 的 pydantic-settings

**Pydantic v2 将 `BaseSettings` 移到了独立的 `pydantic_settings` 包。** 需 `pip install pydantic-settings`，然后 `from pydantic_settings import BaseSettings`。

### 坑 8：`HTMLParser.unescape()` 已移除

**Python 3.9+ 移除了 `HTMLParser.unescape()`，必须用 `html.unescape()` 替代。**

### 坑 9：直接运行 `python app/main.py` 会报错

**`sys.path` 变成 `app/` 目录而非项目根目录。** 当前 `main.py` 已用 `sys.path.insert(0, 项目根目录)` 修复，但仍推荐 `python -m app.main`。

### 坑 10：`os.getenv()` vs Pydantic Settings

**`os.getenv()` 读不到 `.env` 中的配置，因为 `.env` 由 Pydantic 加载不写入 `os.environ`。** 统一通过 `from app.config import settings` 读取。

### 坑 11：async generator 不能 `return` 值

**`ReActAgent._strategy_cycle()` 试图用 `return AgentResult(...)` 返回结果，但 async generator 的 return 值无法被 `async for` 消费。** 必须将结果存到实例变量 `self._result`，消费完事件后通过 `agent.result` 属性获取。

### 坑 12：LLM 和 Agent 的事件构建重复

**`llm_service.py` 和 `base.py` 各自有一套 `_build_*_event()` 方法，事件格式不一致。** 统一到 `app/core/events.py`。

### 坑 13：异质 async generator 增加消费负担

**`async_generate()` 同时 yield `str`（SSE 事件）和 `StreamResult`（数据对象）。** 改为通过参数传入 `StreamResult` 对象，generator 只 yield str。

### 坑 14：LLM 层越界做了 Agent 层的决策

**旧 `async_generate()` 在内部管理 messages 历史 + 判断 finish_reason + 决定是否重试。** 分开：`async_generate()` 只做单轮推理 + 返回原始数据；策略循环由 `ReActAgent._strategy_cycle()` 控制。

### 坑 15（本轮新增）：构造函数的形参名 vs 调用时的关键字参数名不一致

**`structured.py` 中 `_try_extract()` 和 `_fallback_extract()` 的参数叫 `model`，但调用 `generate(model=model)` 时形参名是 `model_key`。** 造成运行时 TypeError。**方法签名改为 `model_key`，调用时也传 `model_key=model_key`。**

### 坑 16（本轮新增）：`**extra` 被静默吞没

**`client.py` 的 `register_config(**extra)` 存入配置后，`get_client()` 只取 `api_key` 和 `base_url`，调用方传入的 `organization`、`timeout`、`max_retries` 等参数永远不会传给 `AsyncOpenAI`。** 定义白名单 `_OPENAI_CLIENT_KWARGS`，`get_client()` 过滤出可传参的字段。

### 坑 17（本轮新增）：半开探针逻辑错误

**`CircuitBreaker` 初始实现中：**
1. **探针计数 off-by-one**：`OPEN→HALF_OPEN` 时不计数，导致 `half_open_max_requests=3` 实际放行 4 个探针
2. **一个探针成功就关闭**：第一个探针成功后 `record_success()` 无条件 `_state=CLOSED`，`half_open_max_requests` 实际只用了 1 个

**修复**：
- `OPEN→HALF_OPEN` 时 `_half_open_requests=1`（当前请求算第一个探针）
- 新增 `_consecutive_successes`，半开下需要**全部探针连续成功**才关闭，任何一个失败回到 OPEN

### 坑 18（本轮新增）：`RETRYABLE` 错误不计入熔断计数

**`classify_error()` 有 `RETRYABLE` 和 `RATE_LIMITED` 两种可重试分类，但 `execute()` 只对 `RATE_LIMITED` 和已删除的 `CIRCUIT_TRIGGER` 调 `record_failure()`。** 意味着超时和 5xx 连续 100 次也不会触发熔断。修复：所有非 `NON_RETRYABLE` 错误都调用 `record_failure()`。

### 坑 19（本轮新增）：`except A, B:` 在 Python 3.14 合法

**`except json.JSONDecodeError, KeyError:` 在 Python 3.14 下编译通过，语义等价 `except (A, B):`**（实测能捕获两者）——交接文档坑 6 的信息已过时。但 3.8~3.13 下这是 `except A as B` 的绑定语法，会静默绑定变量而不捕获 B 类。已全库规范为元组形式 `except (A, B):`，**新代码一律用元组形式，不要在 3.14 上赌它的兼容性**。

### 坑 20（本轮新增）：Windows GBK 控制台 print 崩溃

**Windows 控制台默认 GBK 编码，`print("  ⚠ 错误...")` 里的 `⚠`（U+26A0）不在 GBK 内 → `UnicodeEncodeError`，服务启动直接崩。** 这也是"本地无 Redis/DB 时 app_state.initialize() 走降级路径会崩"的隐藏原因（降级分支打印 `⚠`）。修复：
1. `app/main.py` 入口统一 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`
2. 代码内符号字符（⚠✓✗🔧✅❌）改为 ASCII 占位（[WARN]/[OK]/[TOOL] 等），兜底非 UTF-8 环境

> LLM 返回内容（可能含 emoji）打印到控制台同样会崩，独立脚本（如 `scripts/test_agent.py`）顶部也要 reconfigure。

### 坑 21（本轮新增）：assistant 消息缺失 `tool_calls` → OpenAI 兼容 API 400

**ReAct 循环把 tool 消息回传前，前置 assistant 消息必须带 `tool_calls` 字段（OpenAI 兼容 API 硬性规则）。** 修复前 executor 只回传 content + reasoning_content，DeepSeek 第二轮必报 `400 - Messages with role 'tool' must be a response to a previous message with 'tool_calls'`，Agent 空转重试到迭代上限。修复：`assistant_msg["tool_calls"] = stream_result.tool_calls`（见 `app/core/agent/executor.py` 步骤 2）。已加测试断言防回归。

---

## 附录：快速参考

### 关键文件索引

| 文件 | 重要程度 | 说明 |
|------|---------|------|
| `app/main.py` | ⭐⭐⭐ | FastAPI 入口 |
| `app/config/settings.py` | ⭐⭐⭐ | 全局配置（约 350 行） |
| `app/app_state.py` | ⭐⭐⭐ | 应用状态管理 |
| `app/services/llm_service.py` | ⭐⭐⭐ | LLM Facade |
| `app/services/llm/` | ⭐⭐⭐ | LLM 子包（8 个模块） |
| `app/services/llm/client.py` | ⭐⭐⭐ | 连接池管理（本轮大改） |
| `app/services/llm/retry.py` | ⭐⭐⭐ | 重试+熔断（本轮大改） |
| `app/core/agent/base.py` | ⭐⭐⭐ | Agent 基类 + 数据结构 |
| `app/core/agent/executor.py` | ⭐⭐⭐ | ReAct 执行引擎 |
| `app/core/events.py` | ⭐⭐⭐ | SSE 事件定义 |
| `app/tools/` | ⭐⭐⭐ | 工具系统 |
| `app/api/routes/chat.py` | ⭐⭐ | 聊天 API（需改造接入 ReActAgent） |

### 文档清单

| 文档 | 说明 |
|------|------|
| `docs/config.md` | ✅ 配置模块 |
| `docs/tools.md` | ✅ 工具模块 |
| `docs/agent.md` | ✅ Agent 模块 |
| `docs/llm.md` | ✅ LLM 层总览 |
| `docs/llm_client.md` | ✅ ClientManager 设计（本轮新增） |
| `docs/llm/retry.md` | ✅ RetryHandler 设计（本轮新增，含流程图+推演） |
| `docs/architecture.md` | ❌ 空 |
| `docs/api.md` | ❌ 空 |
| `docs/deployment.md` | ❌ 空 |

### 常用命令

```bash
# 启动服务
uv run python -m app.main

# 运行测试脚本
uv run python -m scripts.test_search_tool
uv run python -m scripts.test_agent

# 验证模块导入
uv run python -c "from app.core.agent import ReActAgent; print('OK')"

# 验证 LLM 子包全部导入
uv run python -c "
from app.services.llm import (
    ClientManager, CircuitBreaker, RetryConfig, RetryHandler,
    StreamParser, StructuredOutput, CostTracker, RateLimiter,
    LLMLogger, LLMRequestRecord,
)
print('OK')
"
```
