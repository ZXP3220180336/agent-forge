# 工具调用统一错误码（ErrorCode）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-17
> **涉及模块**：`app/domain/ports/tool_gateway.py`（ErrorCode + ToolResult.error_code）· `app/integration/tools/executor.py`（各失败路径带码）· `app/integration/tools/security.py`（审计记录 error_code）
> **关联文档**：[tools.md](../../../docs/integration_doc/tools_doc/tools.md) · [executor.md](../../../docs/integration_doc/tools_doc/executor.md) · [security.md](../../../docs/integration_doc/tools_doc/security.md)

---

## Context

- 现状：`ToolResult.error` 是纯中文字符串（如「参数验证失败: 参数 'count' 类型应为 integer」），无结构化错误码。错误首要消费者是 **LLM**（需要自然语言归因自我修正），故历史设计聚焦中文描述
- **产品导向（CLAUDE.md 第一硬性要求）**：本项目产品 = 多 Agent 任务执行引擎 + 良率根因分析（Yield RCA），工程亮点是**证据链 + 置信度分级**——「每个结论可回溯到数据来源」。工具调用失败的类型（超时 vs 参数错误 vs 数据源不可用）决定结论置信度，结构化错误码是「失败可机器分类」的载体，直接服务证据链可审计性
- **真实缺口**：审计 `tool_call` 事件无法按错误类型聚合（只能 grep 字符串）；未来 Agent 层无法按错误类型分支（参数错误→修正重试、超时→检查数据源）；置信度分级无机器可读信号
- **工业参照**：LangChain `ToolException` + `handle_tool_error` 按类型分类处理；MCP `isError` + `structuredContent`——均为**结构化标识 + 人类可读文本并存**，不是二选一

## Decision

**引入 `ErrorCode`（StrEnum，系统级 6 码）+ `ToolResult.error_code` 可选字段 + executor 各失败路径带码 + 审计记录。工具业务错误默认 None（error 字符串承载 LLM 归因）。**

- **枚举**（`domain/ports/tool_gateway.py`，与 ToolResult 同层）：`NOT_REGISTERED` / `JSON_PARSE` / `VALIDATION` / `REJECTED` / `TIMEOUT` / `UNKNOWN`
- **不含 `RETRY_EXHAUSTED`**：最终错误码 = 最后一次失败的类型（TIMEOUT / UNKNOWN / 业务 None），重试次数由 `retry_count` 表达，避免码语义重叠
- **executor 路径映射**：未注册→NOT_REGISTERED / 参数 JSON 解析→JSON_PARSE / 校验→VALIDATION / 审批拒绝→REJECTED / 超时（wait_for）→TIMEOUT / 未捕获异常→UNKNOWN；工具业务失败透传其业务码（默认 None）
- **审计**：`ToolAuditor.record` 记录 `error_code` 字段 → `tool_call` 事件可按错误类型聚合（证据链）
- **向后兼容**：`error_code` 默认 None，现有构造点与测试零破坏；`error` 中文归因保持不变（LLM 体验零变化）
- **明确不做**：内置工具业务错误不带码（error 归因充分，未来按需扩业务码）；Agent 层消费 error_code 做分支 = 未来增强（证据链置信度分级接入时启用）

## Consequences

- **正面**：审计 / 证据链可机器分类工具失败类型（可聚合、可统计）；为 Agent 层分支与置信度分级留结构化信号；错误码与中文归因并存，LLM 归因零损失；向后兼容零破坏
- **负面**：`ToolResult` 领域契约多一个字段（所有构造点需理解其语义）；错误码与 error 字符串双轨存在，需文档明确边界（系统级带码 / 业务级归因）；审计日志多一字段（短枚举名，无隐私面）
