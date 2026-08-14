# agent-forge

> 工业级 AI Agent 系统：多 Agent 任务执行引擎 + 半导体良率异常根因分析（Yield RCA）

基于 FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整 ReAct 循环 Agent（推理 ↔ 工具调用 ↔ 推理），并配套工业级 LLM 可靠性链（熔断 / 限流 / 降级 / 结构化输出）。

**核心能力**：多 Agent 任务执行引擎 + 半导体良率异常根因分析场景（Yield RCA）。产品方向与决策详见 [docs/product.md](docs/product.md)。

## 功能特性

- **完整 ReAct 循环**：推理 → 工具调用 → 推理的闭环 Agent，支持 SSE 流式事件输出
- **LLM 可靠性链**：滑动窗口熔断、错误分类白名单、半开探针、流式整流重试、fallback 降级、RPM/TPM 双桶限流
- **多模型配置**：main / reasoning / fast 三个模型按场景切换（对话 / 深度推理 / 快速任务），连接池按 key 缓存复用
- **结构化输出**：三级降级（JSON Schema → JSON Mode → 正则提取），适配不同模型能力
- **工具系统**：BaseTool + 5 个内置工具（search / readFile / writeFile / code_exec / web_browse），并行执行 + 并发限流
- **服务化会话**：SessionManager / ContextManager / TaskService / EmbeddingService，任务级与工具级并发控制

## 技术栈

| 层 | 技术 |
| --- | --- |
| 语言 | Python ≥ 3.14 |
| 包管理 | uv |
| Web 框架 | FastAPI + Uvicorn |
| 模型协议 | OpenAI API 协议（兼容 DeepSeek） |
| 数据层 | SQLAlchemy (async) + Redis |
| 辅助 | tiktoken（token 计数）、Tavily（搜索）、jsonschema（结构化校验） |

## 架构概览

```text
API 层（FastAPI 路由）→ chat(SSE) / session
    ↓
服务层（LLMService / SessionManager / ContextManager / TaskService / ToolService）
    ↓
核心层（BaseAgent + ReActAgent / Prompts / Events）
    ↓
工具层（search / readFile / writeFile / code_exec / web_browse）
    ↓
配置层（Pydantic Settings，从 .env 加载）
```

完整架构分层、核心调用链路与模块实现状态见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

### 环境要求

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/) 包管理器
- Redis（会话缓存依赖；缺失时服务降级启动）

### 安装与运行

```bash
# 1. 安装依赖
uv sync

# 2. 配置环境变量
cp .env.example .env        # 然后编辑 .env 填入 API Key 等配置

# 3. 启动服务（uvicorn reload，0.0.0.0:8000）
uv run python -m app.main
```

启动后访问 `http://localhost:8000/docs` 查看 API 文档。

### 快速验证

```bash
# 运行全量测试
uv run pytest

# 运行独立验证脚本
uv run python -m scripts.test_search_tool
uv run python -m scripts.test_agent

# 验证模块导入
uv run python -c "from app.core.agent import ReActAgent; print('OK')"
```

## 配置说明

所有配置集中在 `.env`（模板见 `.env.example`），经 [app/config/settings.py](app/config/settings.py) 的 Pydantic Settings 单例加载。配置项分三档：

- **必填**：API Key、模型 ID、数据库/Redis 地址、JWT 密钥等
- **可选**：模型参数、并发数、限流配额、日志级别等
- **内部**：熔断阈值、退避参数等可靠性调优项（settings.py 已设合理默认值，一般无需修改）

详细配置项说明见 [docs/config_doc/config.md](docs/config_doc/config.md)。

> **安全提示**：`.env` 含敏感信息（API Key、JWT 密钥），已被 .gitignore 排除，切勿提交到版本库。

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat/send` | 发送消息（SSE 流式响应，驱动 ReAct Agent 循环） |
| POST | `/api/session` | 创建会话 |
| GET | `/api/session` | 会话列表（分页/搜索） |
| GET | `/api/session/{id}` | 会话详情 |
| DELETE | `/api/session/{id}` | 删除会话 |

> admin / agent / tool 路由与中间件为预留实现，详见 [docs/api_doc/api.md](docs/api_doc/api.md)。

## 测试

```bash
uv run pytest            # 全量测试（214 用例）
```

pytest 已配置 `asyncio_mode = "auto"`，测试函数无需手动标记 `@pytest.mark.asyncio`。

## 文档体系

项目在 `docs/` 建立完整中文文档体系：

- [HANDOFF](docs/HANDOFF.md) — 顶层交接文档（框架级计划 / 进度 / 研发教训）
- [architecture](docs/architecture.md) — 架构分层 + 核心链路 + 模块实现状态
- [product](docs/product.md) — 产品定位与方向（Yield RCA / 工业 RAG / EDA）
- [deployment](docs/deployment.md) — 部署说明（运行方式 / 环境 / 依赖基础设施）
- LLM 层：[service_doc/llm_doc/](docs/service_doc/llm_doc/)（llm 总览 / client / retry / streaming / structure / limiter）
- 各模块：[core_doc](docs/core_doc/) / [service_doc](docs/service_doc/) / [api_doc](docs/api_doc/) / [tool_doc](docs/tool_doc/) / [model_doc](docs/model_doc/) / [infrastructure_doc](docs/infrastructure_doc/) / [config_doc](docs/config_doc/) / [utils_doc](docs/utils_doc/)

## 项目状态

- 配置 / 工具 / LLM 服务层 / 核心 Agent 层已完成（Phase 1-3）
- 基础设施层、MemoryService、admin/agent/tool 路由、中间件为预留（Phase 4-7 进行中）
- 当前进度与遗留问题见 [docs/HANDOFF.md](docs/HANDOFF.md)

## 许可证

[MIT](LICENSE)

Copyright (c) 2026 ZXP3220180336
