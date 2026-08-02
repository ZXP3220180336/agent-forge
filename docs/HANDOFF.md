# 🚩 项目交接文档

> **项目名称**：AI Agent 系统（AsyncioDemo）
> **交接时间**：2026-08-01
> **项目路径**：`e:\MyWorkSpace\Agent\VSCodeDemo\PersonalProject\AsyncioDemo`
> **运行方式**：`uv run python -m app.main`（FastAPI 服务）
> **Python**：3.14 | **包管理**：uv | **平台**：Windows 11
> **代码规模**：约 5040 行 Python
>
> **文档定位**：仅记录框架级信息 —— 整体框架、顶层计划、模块概览、研发教训（通用/项目级）、当前进度。模块级技术细节与教训见各模块说明文档（见文末「文档清单」）。

---

## 1. 我们在做什么

构建一个**工业级 AI Agent 系统**，基于 FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整的 ReAct 循环 Agent。

### 架构分层

```text
API 层（FastAPI 路由）
    ↓
服务层（LLMService / SessionManager / ContextManager / MemoryService / TaskService / ToolService）
    ├── LLM 子包（ClientManager / RetryHandler / StreamParser / RateLimiter / ReservationLimiter / CostTracker / LLMLogger / StructuredOutput）
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

---

## 2. 顶层计划

| Phase   | 模块                                                       | 状态                                                                        |
| ------- | ---------------------------------------------------------- | --------------------------------------------------------------------------- |
| 1       | 配置模块 + 工具模块                                        | ✅ 已完成                                                                   |
| 2       | LLM 服务层重构（8 子模块）                                 | ✅ 已完成（本轮大幅改造 retry.py：熔断工业级、错误分类、半开探针）          |
| 3       | 核心 Agent 层（BaseAgent + ReActAgent + Prompts + Events） | ✅ 已完成（闭环打通）                                                       |
| 4       | 基础设施层（Database/Redis/VectorStore/MessageQueue）      | 🔶 有文件但未验证（asyncpg 驱动未安装，DB 恒降级）                          |
| 5       | 服务层补全（ToolService / MemoryService / TaskService）    | 🔶 ToolService 已实现；TaskService 已实现（并发信号量）；Memory 仍为空文件 |
| 6       | API 路由完善（Admin / Agent / Tool 路由）                  | 🔶 chat 已接入 ReActAgent；Admin/Agent/Tool 路由仍为空文件                  |
| 7       | 测试 + 文档收尾                                            | 🔶 部分完成（48 测试通过：test_retry 24 + test_classify_error 22 + 集成 2） |

---

## 3. 模块概览

> 本节介绍顶层计划各模块的基本信息：在整个 Agent 中的作用、需要实现哪些功能、目前已经实现了哪些功能。模块的设计细节、组件、配置、示例与边界详见各模块说明文档；暂时没有说明文档的模块，等文档建立后再补充链接。

### 3.1 配置模块（Phase 1）✅

- **作用**：系统的配置中心，统一管理所有配置项，供全部模块经 `settings` 单例访问。
- **需要实现**：集中式配置、Pydantic 类型验证、多模型切换、任务优先级与并发控制、LLM 高级配置（重试/熔断/限流/Fallback/抖动）。
- **已经实现**：全部完成 —— 约 90 个配置项、字段验证器、聚合属性、`.env` 加载与优先级。

**详见** [config.md](config.md)

### 3.2 工具模块（Phase 1）✅

- **作用**：为 Agent 提供可执行能力集合，让 LLM 能与外部世界交互（搜索/文件/命令/网页）。
- **需要实现**：统一工具接口、注册中心（参数验证/重试/统计/超时/钩子）、内置工具自动发现。
- **已经实现**：`BaseTool` 抽象基类、`ToolRegistry` 注册中心、5 个内置工具（search / readFile / writeFile / code_exec / web_browse）。

**详见** [tools.md](tools.md)

### 3.3 LLM 服务层（Phase 2）✅

- **作用**：系统的模型通信基础设施，统一封装与大语言模型的所有交互。
- **需要实现**：连接池管理、重试与熔断、流式/非流式解析、结构化输出、请求日志、客户端限流、成本计算、文本向量化。
- **已经实现**：`LLMService` Facade + `app/services/llm/` 子模块（ClientManager / RetryHandler / StreamParser / StructuredOutput / LLMLogger / RateLimiter / ReservationLimiter / CostTracker）+ `EmbeddingService`。限流双文件：`rate_limiter.py`（acquire 形态）+ `reservation_limiter.py`（reserve/settle 形态，独立实现，llm_service 实际使用）。
- **本轮核心改造（2026-08-01，retry.py）**：
  - 熔断判定升级为**滑动窗口错误率模型**（Hystrix 参考），请求级粒度，429 分离
  - 错误分类**白名单映射**（`classify_error`），未知异常默认 NON_RETRYABLE，显式捕获 httpx 网络异常
  - 半开探针**失败一律回 OPEN**（429/超时/5xx 回 OPEN 冷却；4xx 回 OPEN + 抛上层）
  - 流式迭代保护（`llm_service.py` chunk 异常捕获）

**详见** [llm.md](llm/llm.md)（层总览）· [client.md](llm/client.md)（ClientManager）· [retry.md](llm/retry.md)（熔断/错误分类/探针设计 + 修复记录 + 场景推演）

### 3.4 核心 Agent 层（Phase 3）✅

- **作用**：系统的决策与行动核心，编排 LLM 推理和工具调用的循环流程。
- **需要实现**：统一入口 + 策略接口、ReAct 循环（推理→行动→观察）、SSE 事件流、预留 Plan-then-Execute / Reflection 策略。
- **已经实现**：`BaseAgent`（`run()` 统一入口 + `_strategy_cycle()` 策略接口）、`ReActAgent` 执行引擎（LLM 推理 → finish_reason 判断 → tool_calls 执行 → 循环）、AgentState/Context/Result 数据结构、事件层 7 种事件类型、planner/reasoning 预留。

**详见** [agent.md](agent.md)

### 3.5 基础设施层（Phase 4）🔶

- **作用**：抽象封装数据库连接池、Redis 客户端、向量数据库、消息队列的底层操作。
- **需要实现**：Database / Redis / VectorStore / MessageQueue 四类抽象层。
- **已经实现**：`app/infrastructure/` 下文件均为空；数据库与 Redis 实际由 `app/app_state.py` 直接管理（`create_async_engine` + `Redis.from_url`），未经过此层封装。
- **遗留**：`asyncpg` 驱动未安装 → DB 恒降级。

**详见**：暂无文档（待补充）

### 3.6 服务层（Phase 5）🔶

- **作用**：业务调度枢纽，串起会话、上下文、记忆、任务、工具等服务。
- **需要实现**：SessionManager / ContextManager / MemoryService / TaskService / ToolService。
- **已经实现**：`SessionManager`（会话 CRUD + Redis 缓存 + 分页/搜索/统计）、`ContextManager`（Token 计数 + 超限截断）、`ToolService`（工具注册 + 统计）、`TaskService`（任务级并发信号量，`agent_max_concurrent_tasks`）；`MemoryService` 仍为空文件。

**详见**：暂无文档（待补充）

### 3.7 API 路由（Phase 6）🔶

- **作用**：暴露 REST API，鉴权后驱动 Agent 循环响应用户。
- **需要实现**：chat / session / admin / agent / tool 路由 + 认证/限流/错误处理中间件。
- **已经实现**：`chat`（SSE 流式，已接入 ReActAgent）、`session`（CRUD）；`admin` / `agent` / `tool` 路由为空文件，中间件目录（auth / rate_limit / error_handler）为空文件（认证当前由 `dependencies.get_current_user` 模拟实现）。

**详见**：暂无文档（待补充）

### 3.8 测试 + 文档（Phase 7）🔶

- **作用**：质量保障与知识沉淀。
- **需要实现**：单元/集成测试覆盖、完整文档体系。
- **已经实现**：48 测试通过（test_retry 24 + test_classify_error 22 + 集成 2）；配置/工具/Agent/LLM 模块文档齐全；`architecture.md` / `api.md` / `deployment.md` 仍为空。

**详见**：暂无文档（待补充）

---

## 4. 研发教训（通用 / 项目级）

> 通用或项目级的踩坑经验直接记录于此；**模块级教训已融入各模块文档**（见「文档清单」）。以下带 ⏳ 标记的为暂留项目级、待对应模块文档建立后再移入的条目。

### 4.1 通用 / 项目级

- **`__init__.py` 文件名错误**：写成了 `__ini__.py`（少个 t）导致 `ImportError: cannot import name 'XX' from 'app.models' (unknown location)`。出现 `(unknown location)` 的导入报错，**先检查 `__init__.py` 文件名**。
- **测试脚本运行路径**：`uv run .\scripts\test_xxx.py` 会把 `scripts/` 加入 sys.path 找不到 `app` 模块。一律用 `uv run python -m scripts.test_xxx` 从项目根目录运行。
- **`except A, B:` 语法**：Python 3.14 下编译通过且语义等价 `except (A, B):`，但 3.8~3.13 下这是 `except A as B` 的绑定语法，会静默绑定变量而不捕获 B 类。**新代码一律用元组形式 `except (A, B):`**，不要在 3.14 上赌兼容性。
- **直接运行 `python app/main.py` 会报错**：`sys.path` 变成 `app/` 目录而非项目根目录。当前 `main.py` 已用 `sys.path.insert(0, 项目根目录)` 修复，但仍推荐 `python -m app.main`。
- **Windows GBK 控制台 print 崩溃**：`print("⚠ 错误...")` 里的 `⚠`（U+26A0）不在 GBK 内 → `UnicodeEncodeError`，服务启动直接崩。修复：① `app/main.py` 入口统一 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`；② 代码内符号字符（⚠✓✗🔧✅❌）改为 ASCII 占位（[WARN]/[OK]/[TOOL] 等）。LLM 返回内容（可能含 emoji）打印到控制台同样会崩，独立脚本顶部也要 reconfigure。
- **markdown 中文表格 lint**：markdownlint 的 MD060 按字符宽度（中文算 2 格）校验表格对齐，手写中文表格极易误报。用脚本按 east_asian_width 计算列宽自动对齐。**新改表格后重跑对齐脚本。**

### 4.2 暂留项目级（待模块文档建立后移入）

- ⏳ **SQLAlchemy 保留属性 `metadata`**：ORM 模型中 `metadata = Column(JSON)` 导致 `'metadata' is reserved when using the Declarative API` 运行时错误，改名为 `meta`。→ 待数据模型文档
- ⏳ **两个 `declarative_base()` 实例**：`messages.py` 和 `session.py` 各自 `Base = declarative_base()`，导致 FK 引用时 mapper 冲突。统一到 `models/database/base.py` 共享一个 Base。→ 待数据模型文档
- ⏳ **路由导入路径与文件结构不一致**：`api/routes/` 的文件名是 `chat.py`，但 `__init__.py` 写的是 `from .chat_router import ...`；路由内用 `from ..dependencies import ...`，但 `dependencies.py` 在 `app/` 下。解决：`__init__.py` 的导入名匹配实际文件名；路由内用绝对导入 `from app.dependencies import ...`。→ 待 api.md

### 4.3 模块级教训归属

模块级教训已融入各模块文档：配置模块（见 config.md「常见问题」）、Agent 层（见 agent.md「常见问题」）、LLM 层（见 llm.md「常见问题」与 retry.md「classify_error / 测试」）。其中 LLM 层多数教训已被 retry.md / client.md 的正文与附录覆盖，未覆盖部分已补充。

---

## 5. 当前进度

> 本节只记录**整个项目**的整体进度。各模块自身的进度、遗留工作与下一步计划记录在各自的模块文档中（见「文档清单」）。

### 5.1 整体完成度

- **Phase 1-3 已完成**：配置、工具、LLM 服务层、核心 Agent 层全部落地，「用户输入 → LLM 思考 → 工具调用 → LLM 总结 → 回复用户」完整闭环已用真实 API（DeepSeek + Tavily）验证打通。
- **Phase 4-7 部分实现**：基础设施层与中间件为空文件；服务层（Session/Context/Tool/Task 已实现，Memory 空）、API 路由（chat/session 已实现，admin/agent/tool 空）部分落地；测试与文档部分完成。

### 5.2 本轮（2026-08-01）已完成

1. **retry.py 三大改造**：滑动窗口熔断 / 错误分类白名单 / 半开探针失败一律回 OPEN（详见 [retry.md](llm/retry.md)）
2. **流式迭代保护**：`llm_service.py` 流式 chunk 异常捕获
3. **chat_router → ReActAgent 闭环打通**：`ContextManager.build_messages()` → `ReActAgent.run()` → SSE 事件流 → `agent.result` 保存回复；配套实现 `ToolService`、启动注册 5 个内置工具、新增 `get_tool_registry` 依赖注入
4. **测试**：48 测试通过（test_retry 24 + test_classify_error 22 + 集成 2）
5. **git 提交**：`248a86e`（滑动窗口熔断）、`9b8cc37`（错误分类 + 探针 + 流式）

### 5.3 项目级遗留问题（无模块文档归属的部分）

| #   | 问题               | 说明                                                                                                                  |
| --- | ------------------ | --------------------------------------------------------------------------------------------------------------------- |
| 1   | **DB 恒降级**      | `asyncpg` 驱动未安装（`No module named 'asyncpg'`），即使有 PostgreSQL 也无法连接 → 需在 pyproject 依赖中加 `asyncpg` |
| 2   | **基础设施层空**   | `infrastructure/`（database/redis/vector_store/message_queue）全部为空文件；DB/Redis 由 `app_state.py` 直接管理       |
| 3   | **服务层未补全**   | `MemoryService` 仍为空文件（TaskService 已实现并发信号量）                                                            |
| 4   | **API 路由未补全** | `admin.py` / `agent.py` / `tool.py` 为空文件（tool 路由可基于 ToolService 的 stats 实现）                             |
| 5   | **文档未补全**     | `architecture.md` / `api.md` / `deployment.md` 为空                                                                   |
| 6   | **中间件未实现**   | `api/middleware/`（auth/rate_limit/error_handler）为空文件，认证为模拟实现                                            |

> **LLM 层自身遗留**（RateLimiter 未集成、`APIResponseValidationError` 网关容忍决策、流式迭代自动重试决策、`generate_structured` 重复实现）：见 [llm.md](llm/llm.md)「当前进度与遗留」。

### 5.4 下一步方向（项目级）

- **优先 4**：验证基础设施 + 服务层模块 —— 先补依赖 `asyncpg`，逐个验证 `infrastructure/`、`services/` 现有文件，补全 `memory_service` / `task_service`
- **优先 5**：补全缺失模块 —— `api/routes/admin.py` / `agent.py` / `tool.py`（空文件）；`docs/architecture.md` / `api.md` / `deployment.md`（空）；单元测试覆盖仍少

> **LLM 层内部下一步**（集成 RateLimiter 到 `LLMService`、retry 层遗留微调）：见 [llm.md](llm/llm.md)「当前进度与遗留」。

---

## 6. 快速参考

### 关键文件索引

| 文件                                | 重要程度  | 说明                                                  |
| ----------------------------------- | --------- | ----------------------------------------------------- |
| `app/main.py`                       | ⭐⭐⭐    | FastAPI 入口                                          |
| `app/config/settings.py`            | ⭐⭐⭐    | 全局配置（约 350 行）                                 |
| `app/app_state.py`                  | ⭐⭐⭐    | 应用状态管理                                          |
| `app/services/llm_service.py`       | ⭐⭐⭐    | LLM Facade（本轮补流式迭代保护）                      |
| `app/services/llm/`                 | ⭐⭐⭐    | LLM 子包（8 个模块）                                  |
| `app/services/llm/client.py`        | ⭐⭐⭐    | 连接池管理                                            |
| `app/services/llm/retry.py`         | ⭐⭐⭐    | 重试+熔断（本轮大改：滑动窗口熔断/错误分类/半开探针） |
| `app/core/agent/base.py`            | ⭐⭐⭐    | Agent 基类 + 数据结构                                 |
| `app/core/agent/executor.py`        | ⭐⭐⭐    | ReAct 执行引擎                                        |
| `app/core/events.py`                | ⭐⭐⭐    | SSE 事件定义                                          |
| `app/tools/`                        | ⭐⭐⭐    | 工具系统                                              |
| `app/api/routes/chat.py`            | ⭐⭐      | 聊天 API（已接入 ReActAgent）                         |
| `tests/unit/test_retry.py`          | ⭐⭐      | retry 单元测试（24 用例）                             |
| `tests/unit/test_classify_error.py` | ⭐⭐      | 错误分类单测（22 用例，本轮新增）                     |

### 文档清单

| 文档                               | 说明                                                        |
| ---------------------------------- | ----------------------------------------------------------- |
| [config.md](config.md)             | ✅ 配置模块                                                 |
| [tools.md](tools.md)               | ✅ 工具模块                                                 |
| [agent.md](agent.md)               | ✅ Agent 模块                                               |
| [llm/llm.md](llm/llm.md)           | ✅ LLM 层总览                                               |
| [llm/client.md](llm/client.md)     | ✅ ClientManager 设计                                       |
| [llm/retry.md](llm/retry.md)       | ✅ RetryHandler 设计（滑动窗口/错误分类/半开探针/修复记录） |
| [architecture.md](architecture.md) | ❌ 空                                                       |
| [api.md](api.md)                   | ❌ 空                                                       |
| [deployment.md](deployment.md)     | ❌ 空                                                       |

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
    ReservationLimiterManager, LLMLogger, LLMRequestRecord,
)
print('OK')
"
```
