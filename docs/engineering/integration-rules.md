# 集成变更检查与正式契约导航

本文件只定义按变更触发的检查，参数和机制只在相应正式组件文档维护。通用正确性见 [G0](ai-engineering-rules.md#g0)，资源与恢复检查见 [运行时规范](agent-runtime-rules.md)。

| Trigger | 必查责任 | 正式契约 |
|---|---|---|
| 请求构建、模型切换、fallback | 每次实际请求的最终 payload 准入；目标窗口、预留及等待控制；不能用首次检查覆盖后续恢复 | [请求预算](../integration_doc/llm_doc/request_budget.md)、[LLMService](../integration_doc/llm_doc/llm_service.md) |
| retry、整流、续接 | 恢复阶段、预算、已输出判定、EOF/收尾分界；不得重发已完成业务 | [Retry](../integration_doc/llm_doc/retry.md)、[流式恢复](../integration_doc/llm_doc/streaming_rectifier.md) |
| reserve、settle、cancel、usage | 明确调用是否启动、谁持有迟回值、每条退出路径的结算；容量封顶不构成幂等证明 | [Limiter](../integration_doc/llm_doc/limiter.md)、[执行控制](../integration_doc/llm_doc/execution_control.md)、[LLM Facade](../integration_doc/llm_doc/llm.md) |
| 异常、熔断、连接池 | 业务终止与传输失败区分；按实际状态检查探针与关闭责任 | [错误](../integration_doc/llm_doc/error.md)、[Retry](../integration_doc/llm_doc/retry.md)、[Client](../integration_doc/llm_doc/client.md) |
| 结构化输出与工具参数 | 边界分类、schema 副本用途、本地校验及副作用准入 | [结构化输出](../integration_doc/llm_doc/structure.md)、[Validator](../integration_doc/tools_doc/validator.md) |
| 工具执行、审批、文件/网络访问 | 风险标记不等于授权；审批实现、审计和实际防护分别核验，不扩大安全保证 | [工具安全](../integration_doc/tools_doc/security.md)、[Executor](../integration_doc/tools_doc/executor.md)、[内置工具](../integration_doc/tools_doc/builtin_doc/builtin.md) |
| 外部插件、线程/进程与资源关闭 | 信任边界、配置注入、刷新和回收责任；不把本地取消当远端已停止 | [外部工具](../integration_doc/tools_doc/external.md)、[Executor](../integration_doc/tools_doc/executor.md) |
| 输出、统计、日志 | 读取上限和展示截断不同；副作用计数及敏感数据处理在正确边界完成 | [ResultProcessor](../integration_doc/tools_doc/result_processor.md)、[工具安全](../integration_doc/tools_doc/security.md) |
| token、Embedding、RCA 数据 | 端口与装配边界、模拟/真实数据区别；不要把组件存在当已接线 | [TokenCounter](../integration_doc/llm_doc/token_counter.md)、[Embedding](../integration_doc/embedding_doc/embedding.md)、[RCA](../integration_doc/tools_doc/builtin_doc/rca.md) |

验收选取实际调用次数、资源去向、账务不丢不重、终态后无新业务和已发内容不重复等可观察证据。计数含义以对应模块为准，不在此复制默认配置表。原有文档的明确矛盾应在唯一正式文件修正，缺源码证据的事项进入待核验 Issue。

G0-6 观测隔离与 EOF 后异常传播仍有待代码迁移的差异，唯一决定与后续责任见 [治理决策](../../adr/2026-09-12-single-source-governance.md)，不能仅改规范就宣称实现合规。
