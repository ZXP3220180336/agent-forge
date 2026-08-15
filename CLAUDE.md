# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目通用规则（Writer、Reviewer 共用）

1. 语言规范、代码风格、ESLint/Black 规则遵循项目配置
2. 新增代码必须附带单元测试
3. 禁止硬编码密钥、端口、配置
4. 编写/修改标准：编写或者修改文件前先输出实现方案，等待我确认后再写入磁盘。只修改需求指定模块，不要改动无关代码。
5. 评审标准：逻辑正确性、边界条件、异常捕获、性能、安全、可读性

## 项目概览

工业级 AI Agent 系统：FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整 ReAct 循环 Agent（推理 ↔ 工具调用 ↔ 推理）。产品方向为多 Agent 任务执行引擎 + 半导体良率异常根因分析（Yield RCA）。

- **技术栈**：Python ≥ 3.14、uv 包管理、FastAPI、SQLAlchemy(async)、Redis、OpenAI SDK、tiktoken
- **平台**：Windows 11 开发；控制台默认 GBK 编码（见「已知坑」）
- **代码规模**：约 7200 行 Python，214 个测试

## 常用命令

```bash
uv sync                                  # 安装生产依赖 + dev 依赖（pytest/debugpy）
uv run python -m app.main                # 启动服务（uvicorn reload，0.0.0.0:8000）
uv run pytest                            # 全量测试（pyproject.toml 已配 testpaths=tests, asyncio_mode=auto）
uv run pytest tests/unit/test_retry.py   # 运行单个测试文件
uv run pytest tests/unit/test_retry.py -k "熔断"  # 按名过滤用例
uv run python -m scripts.test_search_tool          # 运行独立验证脚本
```

- pytest 采用 `asyncio_mode = "auto"`，测试函数无需手动标记 `@pytest.mark.asyncio`
- 运行脚本一律用 `uv run python -m scripts.xxx`，**不要**用 `uv run ./scripts/xxx.py`（会把 `scripts/` 加入 sys.path 导致找不到 `app` 模块）
- 独立脚本顶部需 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`，否则打印 emoji 到 Windows 控制台会 `UnicodeEncodeError`

## 架构分层

```
接入层 app/api/ → chat(SSE) / session 已实现；admin/agent/tool 路由与中间件为空文件
    ↓
应用层 app/application/ → SessionManager / ContextManager / TaskService
    ↓
领域层 app/domain/ → BaseAgent + ReActAgent / Prompts / Ports；Memory / Reasoning 预留
    ↓
集成层 app/integration/ → LLMService + llm/ 7 组件 / ToolService + 5 内置工具 / EmbeddingService
    ↓
基础设施层 app/infrastructure/ → Session/Message ORM；Database / Redis / VectorStore 待落地（DB/Redis 暂由 container.py 直管）
    ↓
共享内核 app/shared/events.py → SSE 事件定义
    ↓
配置层（app/config/settings.py）→ Pydantic Settings 单例，约 90 配置项，从 .env 加载

装配根 app/container.py → 唯一读 settings，组装各层并注入
```

### 核心调用链路

```
POST /api/chat/send
  → SessionManager（会话验证 + 存用户消息）
  → ContextManager（构建 messages，token 计数/截断）
  → TaskService.run_agent()（任务级并发信号量 agent_max_concurrent_tasks）
      → ReActAgent._strategy_cycle()（ReAct 循环）
          → LLMService.async_generate()（流式 + 重试/熔断/限流/流式整流）
          → ToolService.execute()（并行 asyncio.gather + 工具级信号量）
  → SSE 事件流 → 存 assistant 消息
```

## 关键设计约定（需跨文件理解）

- **策略模式**：`BaseAgent.run()` 统一入口，`_strategy_cycle()` 由子类实现（当前 ReActAgent；Plan-then-Execute / Reflection 预留）。Agent 每次 `run()` 新建无状态实例，上下文经 `AgentContext` 传入。
- **LLM/Agent 分层**：LLM 层管单轮推理与可靠性，Agent 层管循环编排与工具。
- **统一事件流**：LLM 层与 Agent 层共用 [app/shared/events.py](app/shared/events.py) 的 SSE 事件定义（reasoning/message/tool_call/done/error 等）。
- **三个模型配置**：`ClientManager.register_config` 注册 `main` / `reasoning` / `fast` 三个 key，`LLMService` 通过 `model_key` 参数选择；连接池按 key 缓存复用。`container.initialize()` 中注册，`settings.llm_fallback_model_id` 配置 fallback 降级。
- **LLM 可靠性链**（[app/integration/llm/](app/integration/llm/)）：
  - `retry.py`：滑动窗口熔断 + 错误分类白名单（`classify_error`，未知异常默认 NON_RETRYABLE）+ 半开探针
  - `reservation_limiter.py`：reserve/settle 形态限流（请求前预留配额、完成后退差；`llm_service` 实际使用它，acquire 形态已移除）
  - `streaming.py`：StreamParser 流式/非流式解析（提取 content / finish_reason / usage / refusal / tool_calls）
  - `streaming_rectifier.py`：流式整流重试（StreamingRectifier，仅首 token 前中断且异常可恢复才整流）
  - `structured.py`：结构化输出三级降级（JSON Schema strict → JSON Mode → 正则提取）
- **调度与执行解耦**：TaskService 决定「哪个任务何时执行」，Agent 决定「单个任务如何执行」。
- **服务实例管理**：所有共享服务在 [app/container.py](app/container.py) 的 `Container` 单例中初始化/关闭，经 FastAPI lifespan 触发。单点基础设施失败不影响启动（降级 + 警告）。

## 已知坑与约束

- **DB 恒降级**：`asyncpg` 驱动未安装（不在 pyproject 依赖中），即使配置 PostgreSQL 也无法连接，会话/消息无法持久化。
- **Redis 是 HTTP 闭环硬依赖**：Redis 缺失时 container 降级（`redis=None`），但 `SessionManager.create_session()` 直接调 `self.redis.set()` 会抛错——聊天/会话接口需要 Redis 可用。
- **Windows GBK**：日志/控制台含 emoji 或 ⚠✓ 等符号会崩。`main.py` 已统一切 UTF-8；新代码内符号统一用 ASCII 占位（[WARN]/[OK]）。
- **`except A, B:` 语法**：Python 3.14 下编译通过且语义为 `except (A, B)`，但 3.8~3.13 是 `except A as B`。新代码一律用元组形式。
- **SQLAlchemy**：ORM 模型 `metadata` 为保留属性，需命名为 `meta`；全部 ORM 模型必须共享 `models/database/base.py` 的同一个 `declarative_base()`，否则 FK 引用 mapper 冲突。
- **启动方式**：推荐 `uv run python -m app.main`。直接 `python app/main.py` 会因 sys.path 问题报错（main.py 已临时修复，但仍不推荐）。

## 文档体系

项目在 `docs/` 建立完整中文文档体系，**修改模块后需同步对应模块文档**：

- [docs/HANDOFF.md](docs/HANDOFF.md) — 顶层交接文档（框架级计划/进度/研发教训）
- [docs/architecture.md](docs/architecture.md) — 架构分层 + 核心链路 + 模块实现状态总览
- [docs/config_doc/config.md](docs/config_doc/config.md)、[docs/deployment.md](docs/deployment.md) — 配置与部署
- LLM 层：`docs/integration_doc/llm_doc/`（llm 总览 / client / retry / streaming / structure / limiter）
- 各模块说明：`docs/domain_doc/`、`docs/application_doc/`、`docs/integration_doc/`、`docs/infrastructure_doc/`、`docs/shared_doc/`、`docs/api_doc/`、`docs/config_doc/`、`docs/utils_doc/`

当前进度与遗留问题（基础设施层空、MemoryService 空、admin/agent/tool 路由空、中间件空、TaskService 编排待实现）见 HANDOFF.md「当前进度」。
