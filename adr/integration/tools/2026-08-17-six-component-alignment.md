# 工具模块六大子组件对齐 + 选择器全量注入

> **状态**：✅ 已采纳
> **决策日期**：2026-08-17
> **涉及模块**：`app/integration/tools/`（tool_service / registry / selector / validator / executor / result_processor / security）
> **关联文档**：[tools.md](../../../docs/integration_doc/tools_doc/tools.md) · [selector.md](../../../docs/integration_doc/tools_doc/selector.md) · [result_processor.md](../../../docs/integration_doc/tools_doc/result_processor.md)

---

## Context

- 工具模块原为 Facade + 5 组件（registry / executor / stats / hooks / assembler），但对照工业级 Agent 工具模块存在明显差距：无工具选择机制、无统一结果处理（各内置工具内联截断只留前 N）、无风险分级、参数校验仅查「未知参数 + 必填」
- 用户提供「工业级六大子组件」蓝图（注册中心 / 选择器 / 校验器 / 调度器 / 结果处理 / 安全审计），要求网络调研后对比整合、重新设计
- 网络调研结论：六大子组件与工业界一致（Hermes / OpenWorker 三层架构、阿里 ToolOrchestrator、DeepSeek-Harness 调度池印证）；工具选择在小体量（<50）下工业标准是**全量注入 + LLM 原生 Function Calling**，仅 >50 才需「向量召回粗排 + LLM 精排」；结果处理工业标准是 **head+tail 截断 + 截断标记**（AgentScope / pydantic-ai-harness）

## Decision

**工具模块对齐工业级六大子组件：Registry / Selector / Validator / Executor / ResultProcessor / Auditor 六子组件 + Stats / Hooks / Assembler 辅助，ToolService 为唯一 Facade。**

- **选择器只留接口不实现召回**：`ToolSelector` Protocol + `DefaultToolSelector`（全量注入）。当前 5 个工具属小体量，LLM 原生选择即可；>50 工具时实现向量召回，构造期注入 `ToolService(selector=...)`，Facade 与 Agent 零改动
- **结果截断收敛单点**：内置工具删除各自内联截断（只留前 N），统一由 `ResultProcessor` 做 head+tail 截断（默认 7:3，中间标记替换），`max_length=tool.max_output_length`（工具自声明），避免双重截断
- **get_openai_tools 经选择器，get_openai_responses 全量**：后者当前零生产者，保持全量转储（改动成本高于收益）
- **ToolGateway 协议签名不变**：`get_openai_tools()` 保持零参数，选择器构造期注入，Agent / API 层零改动

## Consequences

- **正面**：模块对齐工业级六大子组件，每类职责单一可替换（校验 / 截断 / 审计 / 选择均可独立演进）；内置工具简化（不再各自截断）；选择器为工具膨胀预留升级路径（>50 时换注入实现即可）
- **负面**：组件数增加（9 个），装配与测试面扩大；`get_openai_responses` 与 `get_openai_tools` 选择语义不对称（当前可接受）；head+tail 截断对「中间信息密集」的输出有丢失风险（marker 提示 agent 已截断，可按需调 head_ratio）
