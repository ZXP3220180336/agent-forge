# 集成层说明文档

> **对应代码**：`app/integration/`
> **更新日期**：2026-08-15
> **文档定位**：能力/集成层（`app/integration/`）—— Agent 外部**能力接入**：模型（LLM 网关 + 嵌入）、工具（执行能力）、检索（向量）；是领域端口 `LLMGateway` / `ToolGateway` / `EmbeddingPort` / `VectorStorePort` / `TokenCounter`（待规划）的适配器实现方。
> **实现状态**：LLM（✅ 已实现）· Tools（✅ 已实现）· Embedding（🔶 已实现，未接线）

---

## 📋 目录

- [集成层说明文档](#集成层说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [实现状态总览](#实现状态总览)
  - [LLM 网关](#llm-网关)
  - [工具系统](#工具系统)
  - [Embedding 服务](#embedding-服务)
  - [典型调用链路](#典型调用链路)
  - [配置关联](#配置关联)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

集成层是系统的**能力接入层**，负责 Agent 与外部**能力**（模型 / 工具 / 检索）的交互：

- **LLM 网关**（`llm/`）：与大语言模型的全部通信——连接池 / 重试 / 熔断 / 限流 / 流式解析 / 整流重试 / 结构化输出 / 成本计算
- **工具系统**（`tools/`）：Agent 的可执行能力集合——工具注册 / 执行 / 重试 / 统计 / 钩子 / 内置工具
- **文本向量化**（`embedding/`）：单条 / 批量嵌入 + 内存缓存
- **向量检索**（`vector_store/`，待规划）：Milvus 向量库检索，服务 RAG 历史案例（Phase D）

### 模块结构

```text
app/integration/
├── embedding/                     ← EmbeddingService 文本向量化
│   └── embedding_service.py       ← EmbeddingService（embed / embed_batch / 缓存）
├── llm/                          ← LLM 网关（LLMService Facade + 7 组件）
│   ├── llm_service.py            ← LLMService（统一 Facade，对外入口）
│   ├── client.py                 ← ClientManager 连接池管理
│   ├── retry.py                  ← RetryHandler + CircuitBreaker
│   ├── streaming.py              ← StreamParser 流式解析
│   ├── streaming_rectifier.py    ← StreamingRectifier 流式整流重试
│   ├── structured.py             ← StructuredOutput 结构化输出
│   ├── reservation_limiter.py    ← ReservationLimiter 客户端限流
│   └── cost_tracker.py           ← CostTracker 成本计算
├── tools/                        ← 工具系统（ToolService Facade + 六大子组件 + 内置工具）
    ├── base.py                   ← BaseTool / ToolResult（元数据 + 校验委托 + 生命周期钩子）
    ├── tool_service.py           ← ToolService（统一 Facade，对外入口）
    ├── registry.py               ← ToolRegistry 注册中心（容器 + Schema 导出 + 元数据查询）
    ├── selector.py               ← ToolSelector 选择器（全量注入，预留召回）
    ├── validator.py              ← ParameterValidator 参数校验器（jsonschema 严格校验）
    ├── executor.py               ← ToolExecutor 执行调度器（信号量/重试/超时/校验/截断/审计）
    ├── result_processor.py       ← ResultProcessor 结果处理器（head+tail 截断 + 错误归一化）
    ├── security.py               ← RiskLevel / ToolAuditor 安全审计（分级 + 留痕）
    ├── stats.py                  ← ToolStats / ToolStatsCollector 执行统计
    ├── hooks.py                  ← ExecutionHooks 执行钩子
    ├── assembler.py              ← ToolAssembler 内置工具装配
    ├── loader.py                 ← ExternalToolLoader 外部工具热加载（execute 惰性检查）
    ├── builtin/                  ← 内置工具（自动发现）
    │   ├── search.py             ← "search" 网络搜索（Tavily）
    │   ├── file_ops.py           ← "readFile" / "writeFile" 文件读写
    │   ├── code_exec.py          ← "code_exec" 终端命令执行
    │   ├── web_browse.py         ← "web_browse" 网页抓取
    │   └── rca/                  ← 良率 RCA 场景工具（5 工具，见 builtin_doc/rca.md）
    └── external/                 ← 外部工具（热加载，见 tools_doc/external.md）

└── vector_store/                 ← 向量检索（待规划：Milvus，服务 RAG）
    ├── base.py                   ← 向量库抽象基类
    └── milvus.py                 ← Milvus 实现
```

### 设计原则

1. **Facade 模式**：`LLMService` / `ToolService` 是各自子系统唯一外部入口，内部组件不对外暴露
2. **依赖倒置**：集成层实现领域端口（`LLMGateway` / `ToolGateway` / `EmbeddingPort` / `VectorStorePort` / `TokenCounter`），领域层只依赖抽象；装配根 `container.py` 在启动时注入
3. **三权分立（LLM）**：传输层（连接）/ 可靠性层（重试/熔断/限流/降级）/ 数据层（解析）/ 策略层（整流）/ 治理层（日志/成本）各司其职
4. **God Object 拆分（Tools）**：ToolService 拆为六大子组件（Registry / Selector / Validator / Executor / ResultProcessor / Auditor）+ 辅助组件（Stats / Hooks / Assembler），Facade 聚合
5. **纯函数优先**：`StreamParser` 等解析组件为无状态静态方法，便于测试与整流重试幂等
6. **降级容错**：单组件初始化失败不影响整体启动，仅记录警告并降级

### 依赖关系

```text
领域层（ReActAgent）只依赖端口：LLMGateway / ToolGateway
        ▲                        │ 实现（依赖倒置）
        └── 装配根 container 注入 ┘
                                 ▼
app/integration/
  ├── llm/ ──────────► AsyncOpenAI（OpenAI 兼容 API，如 DeepSeek）
  ├── tools/ ────────► Tavily / httpx / aiofiles / subprocess
  ├── embedding/ ──────────► AsyncOpenAI（/embeddings 端点，复用连接池）
  └── vector_store/ ────────► Milvus（向量库，Phase D）
```

- **外部调用方**：领域层 Agent（经端口）；应用层 / 接入层（经 container 依赖注入）
- **配置**：各子模块经 `register_config()` 注入，零 `settings` 直接依赖（由 container 唯一读取）
- **事件日志**：LLM 调用业务事件 `llm_call` 走全局日志框架（`app/platform/observability/logger.py`）

---

## 实现状态总览

| 子模块 | 文件 | 状态 | 核心内容 |
| --- | --- | --- | --- |
| LLM Facade | `llm/llm_service.py` | ✅ | `LLMService`：`async_generate` / `generate` / `generate_structured` / `calculate_cost` |
| LLM 子包 | `llm/`（7 组件） | ✅ | ClientManager / RetryHandler / StreamParser / StreamingRectifier / StructuredOutput / ReservationLimiter / CostTracker |
| 工具 Facade | `tools/tool_service.py` | ✅ | `ToolService`：注册 / 选择 / 校验 / 执行 / 截断 / 审计 / 统计 / 钩子 / 装配 / Schema 导出 |
| 工具子包 | `tools/`（六大子组件） | ✅ | Registry / Selector / Validator / Executor / ResultProcessor / Auditor + Stats / Hooks / Assembler / Loader |
| 内置工具 | `tools/builtin/` | ✅ | search / readFile / writeFile / code_exec / web_browse + RCA 5 工具（query_batch_yield 等） |
| 外部工具加载器 | `tools/loader.py` | ✅ | ExternalToolLoader：execute 惰性检查热加载 + 生命周期钩子（external/ 目录） |
| Embedding | `embedding/embedding_service.py` | 🔶 已实现未接线 | `EmbeddingService`：`embed` / `embed_batch` / 内存缓存 |
| VectorStore adapter | `vector_store/` | ⬜ 待规划 | Milvus 向量库检索（Phase D，规划接入 RAG） |
| RCA 工具 | `tools/builtin/rca/` | ✅ | query_batch_yield / query_equipment_alerts / query_fdc_params / query_defect_map / search_historical_rca（模拟数据源，见 [rca.md](tools_doc/builtin_doc/rca.md)） |

---

## LLM 网关

**代码**：`app/integration/llm/` · **文档**：[LLM 层详解](llm_doc/llm.md) · [LLMService 编排](llm_doc/llm_service.md)

负责所有与大语言模型的交互，是系统的**模型通信基础设施**。`LLMService` 是唯一外部入口，内部 7 组件各司其职：

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| `ClientManager` | client.py | 全局共享 AsyncOpenAI 连接池，main / reasoning / fast 三档模型懒加载 |
| `RetryHandler` | retry.py | 指数退避 + 抖动 + CircuitBreaker 熔断 + fallback 降级链 |
| `StreamParser` | streaming.py | 逐 chunk 解析流式响应（纯函数，无状态） |
| `StreamingRectifier` | streaming_rectifier.py | 流式整流重试：首 token 前中断才重试（防重复输出 / 双倍计费） |
| `StructuredOutput` | structured.py | 结构化输出三级降级（JSON Schema → JSON Mode → 正则提取） |
| `ReservationLimiter` | reservation_limiter.py | 客户端限流，双 Token Bucket（RPM + TPM），reserve/settle 形态 |
| `CostTracker` | cost_tracker.py | 按模型定价表估算成本（前缀匹配） |

四个对外入口：`generate()`（非流式，简单任务）/ `async_generate()`（流式，用户交互 SSE）/ `generate_structured()`（结构化输出）/ `calculate_cost()`（成本）。

**可靠性链**：限流（reserve/settle）→ 重试/熔断/降级 → 流式整流 → 解析 → 事件日志（`llm_call`）。

## 工具系统

**代码**：`app/integration/tools/` · **文档**：[ToolService 详解](tools_doc/tool_service.md) · [工具模块接口](tools_doc/tools.md)

为 Agent 提供可执行能力集合。`ToolService` 是唯一外部入口（Facade），聚合工业级六大子组件 + 辅助组件：

| 子组件 | 文件 | 职责 |
| --- | --- | --- |
| `ToolRegistry` | registry.py | 注册中心：容器 + Schema 导出 + 按风险/分类查询 |
| `ToolSelector` | selector.py | 选择器：选注入子集（默认全量注入，预留召回） |
| `ParameterValidator` | validator.py | 校验器：jsonschema 严格校验 + 错误归因 |
| `ToolExecutor` | executor.py | 调度器：信号量 / 重试 / 超时 / 校验 / 截断 / 审计编排 |
| `ResultProcessor` | result_processor.py | 结果处理器：head+tail 截断 + 错误归一化 |
| `ToolAuditor` | security.py | 安全审计：风险分级 L0-L3 + 审计留痕 |
| `ToolStatsCollector` | stats.py | 执行统计（调用次数 / 成功率 / 平均耗时） |
| `ExecutionHooks` | hooks.py | 执行钩子（成功路径通知） |
| `ToolAssembler` | assembler.py | 内置工具幂等装配 |
| `ExternalToolLoader` | loader.py | 外部工具热加载（execute 惰性检查 + 生命周期钩子） |

**内置工具**（`builtin/` 自动发现，`BaseTool` 子类即注册）：`search`（Tavily 搜索）/ `readFile` / `writeFile` / `code_exec`（危险命令黑名单 + L2 分级）/ `web_browse`（自实现 HTML→文本解析）/ RCA 5 工具（`query_batch_yield` 等，见 [RCA 工具](tools_doc/builtin_doc/rca.md)）。风险分级见 [安全文档](tools_doc/security.md)。

**外部工具**（`external/` + `ExternalToolLoader`）：文件放入目录即被 `execute` 惰性检查发现，下次调用生效（无后台任务），见 [外部工具热加载](tools_doc/external.md)。

**执行流程**：信号量内 → 查注册表 → 参数校验（jsonschema 归因）→ 重试循环（`asyncio.wait_for` + 指数退避）→ 结果截断（head+tail）→ 统计 + 钩子 + 审计。

## Embedding 服务

**代码**：`app/integration/embedding/` · **文档**：[Embedding 详解](embedding_doc/embedding.md)

文本向量化：`embed()` / `embed_batch()`（自动分批、保持顺序、缓存穿透只请求未命中项），MD5 + 模型名前缀的内存缓存。规划接入长期记忆 / RAG 历史案例检索（Phase D）。

---

## 典型调用链路

```text
用户 → FastAPI 路由 → ReActAgent（领域层，ReAct 循环）
    → LLMGateway.async_generate（集成层：限流 → 重试/熔断 → 整流 → 解析 → 事件）
    → LLM 返回 tool_calls → ToolGateway.execute（集成层：信号量 → 校验 → 执行 → 重试 → 统计）
    → 观察结果回填 → 继续推理 → 最终答案
```

集成层是这条链上「与外部能力打交道」的所有环节，决定系统的**健壮性**（重试 / 熔断 / 限流 / 降级）、**可用性**（工具执行 / 并行）与**可观测性**（调用事件日志、工具统计）。

---

## 配置关联

- LLM 层配置（模型 / 重试 / 熔断 / 限流 / 整流 / 结构化 / 嵌入）见 [LLM 层文档](llm_doc/llm.md)
- 工具配置（超时 / 重试 / 并发 / 输出截断）见 [ToolService 文档](tools_doc/tool_service.md)
- 全部配置项见 [config 文档](../config_doc/config.md)

配置从 `.env` 加载，仅装配根读取，各子模块经 `register_config()` 注入。

---

## 相关文档

- [架构设计](../architecture.md)（集成层在 7 层架构中的位置与演进路径）
- [应用层说明](../application_doc/README.md)
- [领域层说明](../domain_doc/README.md)
- [LLM 层详解](llm_doc/llm.md) · [StreamParser](llm_doc/streaming.md) · [整流策略](llm_doc/streaming_rectifier.md) · [限流](llm_doc/limiter.md) · [结构化](llm_doc/structure.md) · [成本计算](llm_doc/cost_tracker.md)
- [ToolService 详解](tools_doc/tool_service.md) · [工具模块接口](tools_doc/tools.md) · [内置工具详解](tools_doc/builtin_doc/builtin.md) · [外部工具热加载](tools_doc/external.md) · [执行调度](tools_doc/executor.md) · [注册中心](tools_doc/registry.md) · [校验](tools_doc/validator.md) · [结果处理](tools_doc/result_processor.md) · [安全审计](tools_doc/security.md) · [选择器](tools_doc/selector.md) · [统计](tools_doc/stats.md)
- [Embedding 详解](embedding_doc/embedding.md)
- [配置说明](../config_doc/config.md)
