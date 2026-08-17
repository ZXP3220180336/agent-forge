# 工具自声明默认超时（调用方 > 工具 > 全局）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-17
> **涉及模块**：`app/integration/tools/base.py`（BaseTool.timeout）· `app/integration/tools/executor.py`（超时解析优先级）· `app/integration/tools/builtin/*.py`（内置工具声明值）
> **关联文档**：[executor.md](../../../docs/integration_doc/tools_doc/executor.md) · [tools.md](../../../docs/integration_doc/tools_doc/tools.md)

---

## Context

- 原超时仅两层：调用方显式 `execute(timeout=...)` > 全局配置 `tool_timeout=30s`，工具本身无超时声明权
- 不同工具耗时特征差异大：readFile / writeFile 毫秒级（30s 纯浪费），code_exec（编译 / 运行）与未来 SQL / 爬虫 / 模型推理类工具可能数十秒至分钟级（30s 会误杀）——全局单值两头不讨好，工具开发者最清楚自身耗时
- 既有技术债：web_browse 内部 httpx 客户端硬编码 15s，与全局 30s 不一致且不可配（双超时冲突）
- 工业级参照：LangChain `BaseTool` 继承 Runnable 含 `timeout` 属性（工具默认超时，调用方可覆盖）；CrewAI tool 构造含 `timeout` 参数；OpenAI Function Calling schema 无 timeout 概念（超时由宿主执行方决定，故不进 `parameters` schema）

## Decision

**给 `BaseTool` 新增 `timeout` 属性（默认 None），作为「工具自声明的默认超时」，executor 超时解析优先级：调用方显式传入 > 工具自声明 > 全局配置。**

- `BaseTool.timeout`：`int | None`，None = 沿用全局 `tool_timeout`；子类按需覆写
- executor 解析时机移至查得工具之后：`timeout = 调用方显式 or tool.timeout or 全局 tool_timeout`
- 内置工具覆写值：search / web_browse → 15s（web_browse 与内部 httpx 超时一致，消掉双超时冲突）、readFile / writeFile → 5s（本地快）、code_exec → 60s（编译 / 运行放宽）
- **不设全局硬上限（cap）**：信任工具声明，与 `max_output_length`（工具声明资源预算 → 处理器消费）完全同构；LangChain 同款默认值语义，避免 cap 引入「调用方超限怎么办」的覆盖语义复杂度
- 超时不进 `parameters` JSON Schema（对齐 OpenAI：宿主决定超时）

## Consequences

- **正面**：工具开发者声明符合自身耗时特征的默认超时，消除全局单值两头不讨好；web_browse 内部 15s 与外层 15s 一致；编排层仍保留最高控制权（显式 timeout 覆盖一切）；新增工具只需覆写一个属性
- **负面**：工具声明的值被信任，误设超大值会放宽整体超时保护（靠代码审查兜底，文档已在 executor.md 记录）；概念多一层，工具作者需理解三级优先级；`max_retries` 维持两档（调用方 / 全局），未与工具声明对齐（当前无此需求）
