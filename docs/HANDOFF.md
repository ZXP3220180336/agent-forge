# 🚩 项目交接文档

> **项目名称**：AI Agent 系统（AsyncioDemo）
> **交接时间**：2026-08-01
> **项目路径**：`e:\MyWorkSpace\Agent\VSCodeDemo\PersonalProject\AsyncioDemo`
> **运行方式**：`uv run python -m app.main`（FastAPI 服务）
> **Python**：3.14 | **包管理**：uv | **平台**：Windows 11
> **代码规模**：约 5040 行 Python
>
> **文档定位**：仅记录整体框架、顶层计划、已完成模块概览（细节见各模块说明文档）、当前进度四类信息。模块技术细节见各模块文档（见文末「文档清单」），踩坑记录见 [lessons.md](lessons.md)。

---

## 1. 我们在做什么

构建一个**工业级 AI Agent 系统**，基于 FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整的 ReAct 循环 Agent。

### 架构分层

```text
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

---

## 2. 顶层计划

| Phase   | 模块                                                       | 状态                                                                        |
| ------- | ---------------------------------------------------------- | --------------------------------------------------------------------------- |
| 1       | 配置模块 + 工具模块                                        | ✅ 已完成                                                                   |
| 2       | LLM 服务层重构（8 子模块）                                 | ✅ 已完成（本轮大幅改造 retry.py：熔断工业级、错误分类、半开探针）          |
| 3       | 核心 Agent 层（BaseAgent + ReActAgent + Prompts + Events） | ✅ 已完成（闭环打通）                                                       |
| 4       | 基础设施层（Database/Redis/VectorStore/MessageQueue）      | 🔶 有文件但未验证（asyncpg 驱动未安装，DB 恒降级）                          |
| 5       | 服务层补全（ToolService / MemoryService / TaskService）    | 🔶 ToolService 已实现；Memory/Task 仍为空文件                               |
| 6       | API 路由完善（Admin / Agent / Tool 路由）                  | 🔶 chat 已接入 ReActAgent；Admin/Agent/Tool 路由仍为空文件                  |
| 7       | 测试 + 文档收尾                                            | 🔶 部分完成（48 测试通过：test_retry 24 + test_classify_error 22 + 集成 2） |

---

## 3. 已完成模块概览

> 各模块的具体信息项（设计、组件、配置、示例、边界）详见对应模块说明文档，本节仅列完成状态与入口。

### 3.1 配置模块 ✅

统一管理系统全部配置项：Pydantic 类型验证、多模型切换、任务优先级与并发控制、LLM 高级配置（重试/熔断/限流/Fallback/抖动）。导出 `settings` 单例。

**详见** [config.md](config.md)

### 3.2 工具模块 ✅

5 个内置工具（search / readFile / writeFile / code_exec / web_browse）+ 统一 `BaseTool` 抽象基类 + `ToolRegistry` 注册中心（参数验证 / 重试退避 / 执行统计 / 超时保护 / 钩子机制）。

**详见** [tools.md](tools.md)

### 3.3 LLM 层 ✅

`LLMService` 统一 Facade + `app/services/llm/` 8 子模块（ClientManager 连接池 / RetryHandler 重试+熔断 / StreamParser 流式解析 / StructuredOutput 结构化输出 / LLMLogger / RateLimiter / CostTracker）。

**本轮核心改造（2026-08-01，retry.py）**：

- 熔断判定升级为**滑动窗口错误率模型**（Hystrix 参考），请求级粒度，429 分离
- 错误分类**白名单映射**（`classify_error`），未知异常默认 NON_RETRYABLE，显式捕获 httpx 网络异常
- 半开探针**失败一律回 OPEN**（429/超时/5xx 回 OPEN 冷却；4xx 回 OPEN + 抛上层）
- 流式迭代保护（`llm_service.py` chunk 异常捕获）

**详见** [llm.md](llm/llm.md)（层总览）· [client.md](llm/client.md)（ClientManager）· [retry.md](llm/retry.md)（熔断/错误分类/探针设计 + 修复记录 + 场景推演）

### 3.4 核心 Agent 层 ✅

`BaseAgent` 抽象基类（run() 统一入口 + _strategy_cycle() 策略接口）+ `ReActAgent` 执行引擎（LLM 推理 → finish_reason 判断 → tool_calls 执行 → 循环）+ 状态/上下文/结果数据结构 + 预留策略接口（planner / reasoning）。SSE 事件流实时推送（7 种事件类型）。

**详见** [agent.md](agent.md)

### 3.5 其他模块状态

| 模块                           | 状态                                                                |
| ------------------------------ | ------------------------------------------------------------------- |
| 事件层 `app/core/events.py`    | ✅ 7 种事件类型 + 统一构建函数                                      |
| 提示词 `app/core/prompts/`     | ✅ 系统/工具/规划模板                                               |
| EmbeddingService               | ✅ 向量化服务                                                       |
| 记忆系统 `app/core/memory/`    | 🔶 有文件（short/long/working），未验证                             |
| 推理模块 `app/core/reasoning/` | 🔶 有文件（CoT/ReAct/Reflection），未验证                           |
| 数据模型 `models/database/`    | ✅ 有文件（base/messages/session/task/tool_log）                    |
| 基础设施 `infrastructure/`     | 🔶 有文件（database/redis/vector_store/message_queue），未验证      |
| 服务层 `services/`             | 🔶 有文件（tool/memory/task/context/session），memory/task 为空文件 |
| API 路由 `api/routes/`         | 🔶 chat 已接入 ReActAgent；admin/agent/tool 为空文件                |
| 中间件 `api/middleware/`       | ✅ 有文件（auth/rate_limit/error_handler）                          |
| 工具 utils/                    | ✅ 有文件（logger/metrics/exceptions/helpers）                      |
| 应用状态 `app_state.py`        | ✅ 容错初始化                                                       |
| 入口 `main.py`                 | ✅ 路径修复                                                         |

### 3.6 研发教训

跨模块踩坑记录 26 条（含 LLM/retry 相关 11 条），按模块归类。**详见** [lessons.md](lessons.md)

---

## 4. 当前进度

### 4.1 本轮（2026-08-01）已完成

1. **retry.py 三大改造**：滑动窗口熔断 / 错误分类白名单 / 半开探针失败一律回 OPEN（详见 [retry.md](llm/retry.md)）
2. **流式迭代保护**：`llm_service.py` 流式 chunk 异常捕获
3. **chat_router → ReActAgent 闭环打通**：`ContextManager.build_messages()` → `ReActAgent.run()` → SSE 事件流 → `agent.result` 保存回复；配套实现 `ToolService`、启动注册 5 个内置工具、新增 `get_tool_registry` 依赖注入。已用真实 API（DeepSeek + Tavily）验证完整闭环
4. **测试**：48 测试通过（test_retry 24 + test_classify_error 22 + 集成 2）
5. **git 提交**：`248a86e`（滑动窗口熔断）、`9b8cc37`（错误分类 + 探针 + 流式）

### 4.2 遗留未定事项（已讨论未实施）

1. **`APIResponseValidationError` 是否按间歇性网关故障容忍**：当前默认 NON_RETRYABLE（重试无效），如需容忍网关损坏需调整
2. **流式迭代是否自动重试**：当前仅捕获报错 + 错误事件，不自动重试流
3. **RateLimiter 仍未集成**（见 4.3 问题 2）

### 4.3 遗留问题

| #   | 问题                     | 说明                                                                                                                                                                          |
| --- | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **端到端验证**           | 集成测试用 Fake LLM 覆盖桥接逻辑；真实 API 冒烟可跑 `scripts.test_agent`；本地缺 Redis/PostgreSQL 走降级路径，`asyncpg` 驱动未安装 → DB 恒降级（需在 pyproject 加 `asyncpg`） |
| 2   | **RateLimiter 未集成**   | `rate_limiter.py` 已实现（双 Token Bucket，RPM+TPM），但 `LLMService.async_generate()` / `generate()` 未调用，限流"有代码无效果"                                              |
| 3   | **部分模块有文件未验证** | `memory_service`/`task_service` 空文件；`admin`/`agent`/`tool` 路由空文件；`infrastructure/` 未验证；`core/reasoning/`、`core/memory/` 未验证                                 |

### 4.4 下一步计划

| 优先   | 事项                           | 说明                                                                                                                                                             |
| ------ | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2      | retry 层遗留微调               | ① `APIResponseValidationError` 是否容忍网关故障；② 流式迭代是否自动重试                                                                                          |
| 3      | 集成 RateLimiter 到 LLMService | `async_generate()` / `generate()` 调用 ClientManager 前先 `RateLimiter.acquire()`；settings 已有 `llm_main_rpm`/`llm_reasoning_rpm`/`llm_fast_rpm`               |
| 4      | 验证基础设施 + 服务层模块      | 先补依赖 `asyncpg`；逐个验证 `infrastructure/`、`services/` 现有文件；`memory_service`/`task_service` 为空文件待补                                               |
| 5      | 补全缺失模块                   | `api/routes/admin.py`/`agent.py`/`tool.py`（空文件，tool 路由可基于 ToolService stats）；`docs/architecture.md`/`api.md`/`deployment.md`（空）；单元测试覆盖仍少 |

---

## 5. 快速参考

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
| [lessons.md](lessons.md)           | ✅ 研发教训与踩坑记录（26 条）                              |
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
    LLMLogger, LLMRequestRecord,
)
print('OK')
"
```
